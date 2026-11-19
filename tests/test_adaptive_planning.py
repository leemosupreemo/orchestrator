"""Unit and benchmark evaluation tests for the Adaptive Specification & Planning Pipeline:
- Living Feature Spec normalization, provenance, and decision-completeness depth (orchestrator/living_spec.py)
- Deterministic Ambiguity & Risk Engine and lightweight gates (orchestrator/risk_engine.py)
- Two-pass planning, rolling-wave deferred backlog jobs, and mid-execution replan invariants
- 5-scenario planner benchmark suite (sparse, partial, detailed, contradictory, outdated)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ROOT_DIR / "orchestrator" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

from orchestrator import experience_loop, living_spec, prd, risk_engine  # noqa: E402


class LivingSpecTests(unittest.TestCase):
    def test_provenance_tags_are_normalized_from_dicts_and_prefixed_strings(self):
        explicit = living_spec.normalize_item("[explicit] Players can view top 50 scores")
        derived = living_spec.normalize_item({"text": "Uses ScoreStore", "provenance": "code-derived", "source": "Sources/ScoreStore.swift"})
        inferred = living_spec.normalize_item("Show loading spinner while fetching")
        self.assertEqual((explicit["provenance"], explicit["source"]), ("explicit", "user_prompt"))
        self.assertEqual((derived["provenance"], derived["source"]), ("code_derived", "Sources/ScoreStore.swift"))
        self.assertEqual((inferred["provenance"], inferred["source"]), ("inferred", "agent_inference"))

    def test_input_depth_measures_decision_resolution_not_just_length(self):
        sparse = living_spec.assess_input_depth("Add a global leaderboard for Photo mode.")
        self.assertEqual(sparse["depth"], "sparse")
        self.assertEqual(sparse["resolved_count"], 0)

        detailed_short = living_spec.assess_input_depth(
            "Update `wordgame/scoring.py` function `score()`.\n"
            "Acceptance Criteria:\n- 7+ letter words add 10 points -> returns total\n"
            "Constraints: Must not add external dependencies.\n"
            "Reference: https://www.figma.com/design/abc/Scoreboard"
        )
        self.assertEqual(detailed_short["depth"], "detailed")
        self.assertGreaterEqual(detailed_short["resolved_count"], 3)

    def test_conflicts_with_prd_non_goals_and_approved_architecture_are_flagged(self):
        doc = prd.replace_section(
            prd.replace_section(prd.template("WordGame"), "pitch", "A solo word puzzle."),
            "not",
            "- No network play: offline only\n- No live chat",
        )
        arch = {"status": "approved", "stack": {"primary_language": "Swift"}}
        conflicts = living_spec.detect_conflicts(
            "Add live chat and rewrite with Flutter for network play.",
            prd_text=doc,
            arch_record=arch,
        )
        sources = {(c["source_a"], c["source_b"]) for c in conflicts}
        self.assertIn(("user_request", "prd.md#Not this"), sources)
        self.assertIn(("user_request", "docs/architecture.json"), sources)

    def test_negated_phrases_in_request_do_not_trigger_false_prd_conflict(self):
        doc = prd.replace_section(prd.template("WordGame"), "not", "- No network play")
        conflicts = living_spec.detect_conflicts("Add a local high-score list with no network play.", prd_text=doc)
        self.assertEqual(conflicts, [])


class RiskEngineTests(unittest.TestCase):
    def test_low_impact_reversible_decisions_choose_defaults_and_become_assumptions(self):
        spec = living_spec.normalize({
            "pass": "spec",
            "title": "[UI] Leaderboard button",
            "intent": "Add leaderboard button",
            "decisions": [{
                "question": "Which button style and loading state should be used?",
                "proposed_default": "Use existing primary button style and skeleton loading state",
                "confidence": "low",
                "impact": "low",
                "reversibility": "easy",
                "resolvable_by": "default_assumption",
            }],
        }, raw_input="Add leaderboard button")
        res = risk_engine.evaluate(spec)
        self.assertTrue(res["intent_gate_passed"])
        self.assertEqual(res["behavior"], "assume_default")
        self.assertEqual(res["clarification_question"], "")
        self.assertTrue(any("existing primary button style" in a["text"] for a in spec["assumptions"]))

    def test_high_impact_privacy_or_fairness_decisions_require_human_clarification(self):
        # Even if a model mislabels exposing private account data or tie-breaker fairness as low impact,
        # the deterministic guardrail upgrades it to high impact and pauses at the Intent Gate.
        spec = living_spec.normalize({
            "pass": "spec",
            "title": "[Leaderboard] Global rankings",
            "intent": "Add a global leaderboard for Photo mode",
            "decisions": [
                {
                    "question": "Should the leaderboard expose private account email addresses?",
                    "proposed_default": "Show display name only",
                    "confidence": "low",
                    "impact": "low",  # intentionally mislabelled by model
                    "reversibility": "easy",
                    "resolvable_by": "default_assumption",
                },
                {
                    "question": "How should ties be resolved and do retries count toward ranking?",
                    "proposed_default": "Earliest timestamp wins",
                    "confidence": "low",
                    "impact": "low",  # intentionally mislabelled by model
                    "reversibility": "easy",
                    "resolvable_by": "default_assumption",
                },
            ],
        }, raw_input="Add a global leaderboard for Photo mode")
        res = risk_engine.evaluate(spec)
        self.assertFalse(res["intent_gate_passed"])
        self.assertEqual(res["behavior"], "clarify")
        self.assertEqual(len(res["clarifications_needed"]), 2)
        self.assertIn("expose private account", res["clarification_question"])
        self.assertIn("How should ties be resolved", res["clarification_question"])

    def test_technically_investigable_decisions_are_resolved_from_repo_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            pkg = root / "wordgame"
            pkg.mkdir()
            (pkg / "scoring.py").write_text("def calculate_photo_score(points: int) -> int:\n    return points * 2\n")
            spec = living_spec.normalize({
                "pass": "spec",
                "title": "[Photo] Leaderboard",
                "intent": "Use existing photo score calculation",
                "decisions": [{
                    "question": "Where is the existing photo score calculation function located?",
                    "proposed_default": "Use calculate_photo_score",
                    "confidence": "low",
                    "impact": "low",
                    "reversibility": "easy",
                    "resolvable_by": "repo_inspection",
                }],
            }, raw_input="Add photo score leaderboard", root=root)
            res = risk_engine.evaluate(spec, root=root)
            self.assertTrue(res["intent_gate_passed"])
            self.assertEqual(res["behavior"], "investigate")
            self.assertEqual(len(res["investigations"]), 1)
            self.assertIn("wordgame/scoring.py", res["investigations"][0]["evidence"])


class RollingWaveAndEvidenceTests(unittest.TestCase):
    def test_outdated_file_paths_are_flagged_with_real_repo_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            (root / "src" / "leaderboard").mkdir(parents=True)
            (root / "src" / "leaderboard" / "score_service.py").write_text("def rank_scores(): pass\n")
            spec = living_spec.normalize(
                {"pass": "spec", "title": "Update score service"},
                raw_input="Update `legacy/old_dir/score_service.py` to sort descending.",
                root=root,
            )
            self.assertEqual(len(spec["stale_references"]), 1)
            self.assertEqual(spec["stale_references"][0]["path"], "legacy/old_dir/score_service.py")
            self.assertIn("src/leaderboard/score_service.py", spec["stale_references"][0]["replacements"])

    def test_deferred_slices_create_waiting_backlog_jobs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / ".orchestrator"
            parent_job = {
                "job_id": "20261118-feature-1",
                "title": "[Leaderboard] Global Photo Leaderboard",
                "feature": "photo-leaderboard",
                "plan": {
                    "tasks": [{"title": "Show top 10 photo scores"}],
                    "deferred": [
                        "Friends-only leaderboard filter: Filter scores by followed friends",
                        "Weekly season reset and badges",
                    ],
                },
            }
            created_first = living_spec.create_deferred_backlog_jobs(runtime, parent_job)
            self.assertEqual(len(created_first), 2)
            created_second = living_spec.create_deferred_backlog_jobs(runtime, parent_job)
            self.assertEqual(created_first, created_second)

            backlog_job = json.loads((runtime / "jobs" / f"{created_first[0]}.json").read_text())
            self.assertEqual(backlog_job["status"], "backlog")
            self.assertEqual(backlog_job["deferred_from"], "20261118-feature-1")
            self.assertEqual(backlog_job["feature"], "photo-leaderboard")

    def test_mid_execution_replan_blocks_dropping_explicit_acceptance_criteria(self):
        spec = living_spec.normalize({
            "pass": "spec",
            "title": "Leaderboard",
            "acceptance_criteria": [
                {"text": "Displays top 50 player scores sorted descending", "provenance": "explicit", "source": "user_prompt"},
                {"text": "Highlights the current player's row", "provenance": "explicit", "source": "user_prompt"},
            ],
            "non_goals": [
                {"text": "No real-money wagering", "provenance": "explicit", "source": "prd.md#Not this"},
            ],
        }, raw_input="Leaderboard with top 50 scores and current player highlight")

        # Valid replan that keeps both explicit criteria (or defers one)
        valid_plan = {
            "summary": "Show top 50 player scores sorted descending",
            "tasks": [{"title": "Render top 50 scores and highlight current player row", "acceptance_criteria": ["Top 50 scores", "Current player row"]}],
        }
        self.assertEqual(living_spec.validate_replan_invariants(spec, valid_plan), [])

        # Invalid replan that silently drops "Highlights the current player's row" and adds "real-money wagering"
        bad_plan = {
            "summary": "Show top 50 player scores and add real-money wagering",
            "tasks": [{"title": "Render top 50 scores", "acceptance_criteria": ["Top 50 scores sorted descending"]}],
        }
        violations = living_spec.validate_replan_invariants(spec, bad_plan)
        self.assertEqual(len(violations), 2)
        self.assertTrue(any("Highlights the current player's row" in v for v in violations))
        self.assertTrue(any("real-money wagering" in v for v in violations))


class PlanGateAndTwoPassTests(unittest.TestCase):
    def test_small_clean_low_risk_plan_auto_passes_plan_gate(self):
        spec = living_spec.normalize({"pass": "spec", "title": "Add badge", "intent": "Show bonus badge"}, raw_input="Show bonus badge")
        risk = risk_engine.evaluate(spec)
        small_plan = {
            "tasks": [{
                "title": "Render bonus badge on score screen",
                "description": "Show badge when bonus > 0",
                "acceptance_criteria": ["Badge is visible when bonus > 0"],
                "likely_files": ["wordgame/scoring.py", "tests/test_scoring.py"],
            }],
        }
        gate = risk_engine.evaluate_plan_gate(spec, risk, small_plan, verification={"status": "approved", "comments": "ok"})
        self.assertTrue(gate["plan_gate_passed"])
        self.assertTrue(gate["auto_pass"])
        self.assertEqual(gate["gate_policy"], "auto_pass")

    def test_manifest_changes_or_architect_concerns_disable_auto_pass(self):
        spec = living_spec.normalize({"pass": "spec", "title": "Add dep", "intent": "Add package"}, raw_input="Add package")
        risk = risk_engine.evaluate(spec)
        manifest_plan = {
            "tasks": [{
                "title": "Add dependency and wire screen",
                "acceptance_criteria": ["Screen renders"],
                "likely_files": ["Package.swift", "Sources/Screen.swift"],
            }],
        }
        gate = risk_engine.evaluate_plan_gate(spec, risk, manifest_plan, verification={"status": "approved", "comments": "ok"})
        self.assertTrue(gate["plan_gate_passed"])
        self.assertFalse(gate["auto_pass"])
        self.assertEqual(gate["gate_policy"], "human_approval")

    def test_two_pass_execution_runs_pass_a_then_pass_b_and_stops_early_on_intent_gate(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_tmp:
            root = Path(tmp)
            old_env = os.environ.copy()
            old_modules = dict(sys.modules)
            try:
                os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(root)
                os.environ["ORCHESTRATOR_RUNTIME_DIR"] = ".orchestrator"
                os.environ["ORCHESTRATOR_USER_STATE_DIR"] = state_tmp
                subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
                (root / "README.md").write_text("# Test")
                for mod in [m for m in sys.modules if m.startswith("orchestrator") or m in ("common", "new_job")]:
                    del sys.modules[mod]
                from orchestrator import cli
                cli.main(["init", "--root", str(root), "--project-name", "Test", "--base-branch", "main"])
                for mod in [m for m in sys.modules if m.startswith("orchestrator") or m in ("common", "new_job")]:
                    del sys.modules[mod]

                from orchestrator.scripts import common, new_job

                def make_paths(job_id: str):
                    jdir = root / ".orchestrator" / "jobs"
                    odir = root / ".orchestrator" / "output" / job_id
                    jdir.mkdir(parents=True, exist_ok=True)
                    odir.mkdir(parents=True, exist_ok=True)
                    return common.JobPaths(job_file=jdir / f"{job_id}.json", brief_file=odir / "brief.md", output_dir=odir)

                # Case 1: Pass A raises a high-impact decision -> stops at Intent Gate after 1 LLM call (Pass B skipped!)
                calls_blocking: list[str] = []

                def fake_llm_intent_stop(model, prompt, *a, **k):
                    calls_blocking.append(prompt)
                    return (json.dumps({
                        "pass": "spec",
                        "title": "[Leaderboard] Photo Mode",
                        "intent": "Add a global leaderboard for Photo mode",
                        "decisions": [{
                            "question": "What constitutes an eligible score and how should ties be resolved?",
                            "proposed_default": "Highest score wins, earliest timestamp breaks ties",
                            "confidence": "low",
                            "impact": "high",
                            "reversibility": "costly",
                            "resolvable_by": "human_decision",
                        }],
                    }), "mock-model", "sid-a")

                with patch.object(new_job, "ROOT", root), \
                     patch.object(new_job, "make_job_paths", side_effect=make_paths), \
                     patch.object(new_job, "run_llm", side_effect=fake_llm_intent_stop), \
                     patch("sys.stdin", io.StringIO("")):
                    new_job.main(["feature", "--summary", "Add a global leaderboard for Photo mode", "--branch-mode", "manual",
                                  "--no-dispatch", "--allowed-models", "gemini", "--allowed-machines", "local"])

                self.assertEqual(len(calls_blocking), 1, "Pass B must not run when Pass A stops at the Intent Gate")
                job_file = next((root / ".orchestrator" / "jobs").glob("*.json"))
                paused_job = json.loads(job_file.read_text())
                self.assertEqual(paused_job["status"], "human-needed")
                self.assertIn("What constitutes an eligible score", paused_job["human_clarification_question"])
                self.assertEqual(paused_job["risk_assessment"]["gate_policy"], "intent_gate")
                job_file.unlink()

                # Case 2: Pass A resolves low-impact assumptions -> chains into Pass B (2 LLM calls) -> produces tasks & spec artifacts
                calls_two_pass: list[str] = []
                replies = [
                    json.dumps({
                        "pass": "spec",
                        "title": "[Leaderboard] Photo Mode Slice 1",
                        "intent": "Show top 10 photo scores",
                        "functional_requirements": [{"text": "Display top 10 photo scores", "provenance": "explicit", "source": "user_prompt"}],
                        "acceptance_criteria": [{"text": "Top 10 scores render in descending order", "provenance": "explicit", "source": "user_prompt"}],
                        "decisions": [{
                            "question": "Which loading state style should be used?",
                            "proposed_default": "Use existing skeleton loader",
                            "confidence": "low",
                            "impact": "low",
                            "reversibility": "easy",
                            "resolvable_by": "default_assumption",
                        }],
                        "stories": [
                            {"id": "story-1", "title": "Top 10 scores list", "summary": "Show top 10 scores", "slice": "MVP",
                             "acceptance_criteria": ["Top 10 scores render in descending order"]},
                            {"id": "story-2", "title": "Friends filter", "summary": "Filter by friends", "slice": "Next",
                             "acceptance_criteria": ["Friends tab filters list"]},
                        ],
                        "ready_story_id": "story-1",
                    }),
                    json.dumps({
                        "title": "[Leaderboard] Photo Mode Slice 1",
                        "summary": "Show top 10 photo scores",
                        "tasks": [{
                            "title": "Render top 10 photo scores on leaderboard screen",
                            "description": "Query top 10 scores and render list",
                            "acceptance_criteria": ["Top 10 scores render in descending order"],
                            "likely_files": ["README.md"],
                            "tests": ["test_top_10_scores"],
                            "complexity": "small",
                        }],
                        "test_cases": [{
                            "title": "Top 10 scores render descending",
                            "type": "unit",
                            "expected": "10 scores sorted descending",
                            "covers": ["Top 10 scores render in descending order"],
                            "tests": ["test_top_10_scores"],
                        }],
                    }),
                ]

                def fake_llm_two_pass(model, prompt, *a, **k):
                    calls_two_pass.append(prompt)
                    return (replies[min(len(calls_two_pass) - 1, len(replies) - 1)], "mock-model", f"sid-{len(calls_two_pass)}")

                with patch.object(new_job, "ROOT", root), \
                     patch.object(new_job, "make_job_paths", side_effect=make_paths), \
                     patch.object(new_job, "run_llm", side_effect=fake_llm_two_pass), \
                     patch("sys.stdin", io.StringIO("")):
                    new_job.main(["feature", "--summary", "Add a global leaderboard for Photo mode", "--branch-mode", "manual",
                                  "--no-dispatch", "--allowed-models", "gemini", "--allowed-machines", "local"])

                self.assertEqual(len(calls_two_pass), 2, "Expected Pass A followed by Pass B")
                self.assertIn("### PASS A: LIVING FEATURE SPEC", calls_two_pass[0])
                self.assertIn("### PASS B: EXECUTION PLAN FROM LIVING FEATURE SPEC ###", calls_two_pass[1])
                planned_job = json.loads(next((root / ".orchestrator" / "jobs").glob("*.json")).read_text())
                self.assertEqual(planned_job["status"], "planned")
                self.assertTrue(planned_job["auto_passed_gates"])
                self.assertIn("Friends filter: Filter by friends", planned_job["plan"]["deferred"])
                self.assertTrue(any("existing skeleton loader" in a for a in planned_job["plan"]["assumptions"]))
                out_dir = root / ".orchestrator" / "output" / planned_job["job_id"]
                self.assertTrue((out_dir / "spec.json").is_file())
                self.assertTrue((out_dir / "spec.md").is_file())
            finally:
                sys.modules.clear()
                sys.modules.update(old_modules)
                os.environ.clear()
                os.environ.update(old_env)


class PlannerBenchmarkEvaluationTests(unittest.TestCase):
    """5-scenario evaluation suite measuring the Adaptive Specification & Planning Pipeline across:
    1) Sparse input
    2) Partial input
    3) Detailed input
    4) Contradictory input
    5) Outdated input (stale file references)
    """

    def test_scenario_1_sparse_input_separates_low_risk_defaults_from_material_decisions(self):
        raw = "Add a global leaderboard for Photo mode."
        spec = living_spec.normalize({
            "pass": "spec",
            "title": "[Leaderboard] Global Photo Leaderboard",
            "intent": "Allow players to view a global leaderboard for Photo mode",
            "functional_requirements": [
                {"text": "Global leaderboard view for Photo mode", "provenance": "explicit", "source": "user_prompt"},
                {"text": "Follow existing UI design patterns and provide loading, empty, and error states", "provenance": "inferred", "source": "agent_inference"},
            ],
            "acceptance_criteria": [
                {"text": "Photo mode players can open the global leaderboard screen", "provenance": "explicit", "source": "user_prompt"},
            ],
            "decisions": [
                {
                    "question": "Which loading, empty, and error UI pattern should the leaderboard use?",
                    "proposed_default": "Follow existing list loading/empty/error state components",
                    "confidence": "low",
                    "impact": "low",
                    "reversibility": "easy",
                    "resolvable_by": "default_assumption",
                },
                {
                    "question": "What constitutes an eligible Photo mode score and how should ties be resolved?",
                    "proposed_default": "Completed rounds only; ties broken by earliest completion timestamp",
                    "confidence": "low",
                    "impact": "high",
                    "reversibility": "costly",
                    "resolvable_by": "human_decision",
                },
            ],
        }, raw_input=raw)
        self.assertEqual(spec["planning_depth"], "sparse")
        res = risk_engine.evaluate(spec)
        # Low-risk UI pattern became a documented assumption, while material scoring fairness paused at the Intent Gate
        self.assertFalse(res["intent_gate_passed"])
        self.assertEqual(res["behavior"], "clarify")
        self.assertEqual(len(res["auto_assumed"]), 1)
        self.assertEqual(len(res["clarifications_needed"]), 1)
        self.assertIn("eligible Photo mode score", res["clarification_question"])

    def test_scenario_2_partial_input_investigates_repo_and_proceeds_without_unnecessary_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            (root / "wordgame").mkdir()
            (root / "wordgame" / "scoring.py").write_text("def score_word(word: str) -> int:\n    return len(word)\n")
            raw = (
                "Add a long-word bonus in Photo mode.\n"
                "Acceptance Criteria:\n"
                "- Words with 7+ letters award 10 bonus points\n"
                "- Bonus points appear in the round summary"
            )
            spec = living_spec.normalize({
                "pass": "spec",
                "title": "[Scoring] Long-Word Bonus",
                "intent": "Award 10 bonus points for 7+ letter words",
                "acceptance_criteria": [
                    {"text": "Words with 7+ letters award 10 bonus points", "provenance": "explicit", "source": "user_prompt"},
                    {"text": "Bonus points appear in the round summary", "provenance": "explicit", "source": "user_prompt"},
                ],
                "decisions": [{
                    "question": "Which scoring module calculates word points?",
                    "proposed_default": "Use score_word in wordgame/scoring.py",
                    "confidence": "low",
                    "impact": "low",
                    "reversibility": "easy",
                    "resolvable_by": "repo_inspection",
                }],
            }, raw_input=raw, root=root)
            self.assertEqual(spec["planning_depth"], "partial")
            res = risk_engine.evaluate(spec, root=root)
            self.assertTrue(res["intent_gate_passed"])
            self.assertEqual(res["clarification_question"], "")
            self.assertEqual(len(res["investigations"]), 1)

    def test_scenario_3_detailed_prd_produces_high_precision_plan_with_zero_unnecessary_assumptions(self):
        raw = (
            "Update `wordgame/scoring.py` function `score_word()`.\n"
            "Acceptance Criteria:\n"
            "- Given a 7-letter word -> `score_word` adds 10 bonus points\n"
            "- Given a 6-letter word -> `score_word` adds 0 bonus points\n"
            "Constraints: Must not modify external dependencies or network behavior.\n"
            "Reference: https://github.com/example/wordgame/issues/42"
        )
        spec = living_spec.normalize({
            "pass": "spec",
            "title": "[Scoring] 7-Letter Bonus in score_word",
            "intent": "Add 10 bonus points for words with 7+ letters in score_word()",
            "functional_requirements": [
                {"text": "Add 10 bonus points in wordgame/scoring.py score_word() when len(word) >= 7", "provenance": "explicit", "source": "user_prompt"},
            ],
            "acceptance_criteria": [
                {"text": "7-letter word adds 10 bonus points", "provenance": "explicit", "source": "user_prompt"},
                {"text": "6-letter word adds 0 bonus points", "provenance": "explicit", "source": "user_prompt"},
            ],
            "constraints": [
                {"text": "Must not modify external dependencies or network behavior", "provenance": "explicit", "source": "user_prompt"},
            ],
            "decisions": [],
        }, raw_input=raw)
        self.assertEqual(spec["planning_depth"], "detailed")
        res = risk_engine.evaluate(spec)
        self.assertTrue(res["intent_gate_passed"])
        self.assertEqual(res["behavior"], "proceed")
        self.assertEqual(spec["assumptions"], [])

    def test_scenario_4_contradictory_input_halts_at_intent_gate_and_names_both_sources(self):
        doc = prd.replace_section(prd.template("WordGame"), "not", "- No network play\n- No user accounts")
        raw = "Add online multiplayer matchmaking with network play and user accounts."
        spec = living_spec.normalize({
            "pass": "spec",
            "title": "[Multiplayer] Online Matchmaking",
            "intent": "Add network play matchmaking",
        }, raw_input=raw, prd_text=doc)
        res = risk_engine.evaluate(spec)
        self.assertFalse(res["intent_gate_passed"])
        self.assertEqual(res["behavior"], "stop_conflict")
        self.assertIn("prd.md#Not this", res["clarification_question"])

    def test_scenario_5_outdated_input_flags_stale_paths_and_records_real_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            (root / "wordgame" / "engine").mkdir(parents=True)
            (root / "wordgame" / "engine" / "rules.py").write_text("def check_word(): return True\n")
            raw = "Update `old_pkg/rules.py` so `check_word()` rejects hyphenated words."
            spec = living_spec.normalize({
                "pass": "spec",
                "title": "[Rules] Reject Hyphenated Words",
                "intent": "Reject hyphenated words in check_word()",
            }, raw_input=raw, root=root)
            risk_engine.evaluate(spec, root=root)
            self.assertEqual(len(spec["stale_references"]), 1)
            self.assertEqual(spec["stale_references"][0]["path"], "old_pkg/rules.py")
            self.assertIn("wordgame/engine/rules.py", spec["stale_references"][0]["replacements"])
            self.assertTrue(any("old_pkg/rules.py" in a["text"] for a in spec["assumptions"]))


class JobStateAndDefaultTests(unittest.TestCase):
    def test_job_state_follows_deferred_slices_conflicts_and_replan_violations(self):
        from orchestrator.web import server

        deferred_state = server.job_state({
            "status": "backlog",
            "deferred_from": "20261118-feature-1",
            "plan": {"tasks": []},
        })
        self.assertEqual(deferred_state["label"], "Deferred slice · no plan yet")
        self.assertIn("Deferred from an earlier slice", deferred_state["reason"])
        self.assertEqual(deferred_state["next"]["action"], "plan_epic")

        conflict_state = server.job_state({
            "status": "human-needed",
            "human_clarification_question": "Conflicting requirements detected: network play vs prd.md#Not this",
            "risk_assessment": {"behavior": "stop_conflict"},
            "plan": {"tasks": []},
        })
        self.assertEqual(conflict_state["label"], "Question for you")
        self.assertIn("conflicting requirements", conflict_state["reason"])

        replan_state = server.job_state({
            "status": "human-needed",
            "human_clarification_question": "Mid-execution replan cannot silently alter approved product requirements",
            "plan": {"tasks": [], "replan_violations": ["Dropped explicit requirement"]},
        })
        self.assertEqual(replan_state["label"], "Question for you")
        self.assertIn("before changing approved requirements", replan_state["reason"])

    def test_every_feature_has_success_criteria_and_kpi_guardrails_respect_recommend_once_then_ignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / ".orchestrator"

            # 1) First feature with no connected analytics app:
            #    - Has implied success_criteria
            #    - Recommends KPI / analytics setup ONCE
            spec_1 = living_spec.normalize(
                {"pass": "spec", "title": "[Photo] Global Leaderboard", "intent": "Show global scores in Photo mode"},
                raw_input="Add a global leaderboard for Photo mode.",
                root=root,
            )
            self.assertTrue(spec_1["success_criteria"], "Every feature must have success criteria (explicit or implied)")
            self.assertEqual(spec_1["success_criteria"][0]["provenance"], "inferred")
            self.assertEqual(spec_1["kpi_metrics"]["status"], "recommend_setup_once")
            self.assertIn("No analytics app is connected yet", spec_1["kpi_metrics"]["recommendation"])
            plan_1 = living_spec.merge_spec_into_plan({"tasks": []}, spec_1, {"gate_policy": "human_approval"})
            self.assertIn("kpi_setup_recommendation", plan_1)

            # 2) Second feature while analytics is STILL not connected:
            #    - Still has success_criteria
            #    - Silently ignores KPIs (no repeated recommendation)
            spec_2 = living_spec.normalize(
                {"pass": "spec", "title": "[Photo] Daily Challenge", "intent": "Add daily challenge prompt"},
                raw_input="Add daily challenge prompt.",
                root=root,
            )
            self.assertTrue(spec_2["success_criteria"])
            self.assertEqual(spec_2["kpi_metrics"]["status"], "ignored_until_connected")
            self.assertEqual(spec_2["kpi_metrics"]["recommendation"], "")
            self.assertIsNone(spec_2["kpi_metrics"]["primary_kpi"])
            self.assertEqual(spec_2["kpi_metrics"]["guardrail_kpis"], [])
            plan_2 = living_spec.merge_spec_into_plan({"tasks": []}, spec_2, {"gate_policy": "human_approval"})
            self.assertNotIn("kpi_setup_recommendation", plan_2)

            # 3) Connect Mixpanel + Sentry in Setup (.orchestrator/config/settings.json):
            #    - KPIs + guardrail / counter-KPI metrics activate automatically and tie to success_criteria
            cfg_dir = runtime / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "settings.json").write_text(json.dumps({
                "analytics_provider": "mixpanel",
                "analytics_key": "MP_SECRET",
                "analytics_region": "us",
                "integrations": {"sentry": {"token": "SENTRY_SECRET"}},
                "kpi_setup_recommended_once": True,
            }))
            spec_3 = living_spec.normalize(
                {
                    "pass": "spec",
                    "title": "[Photo] Share Score Card",
                    "intent": "Allow players to share their Photo mode score card",
                    "success_criteria": [
                        {"text": "At least 25% of completed Photo rounds trigger a score card share", "provenance": "explicit", "source": "user_prompt"},
                    ],
                },
                raw_input="Allow players to share their Photo mode score card. Success Criteria: 25% share rate.",
                root=root,
            )
            self.assertEqual(spec_3["success_criteria"][0]["provenance"], "explicit")
            km = spec_3["kpi_metrics"]
            self.assertEqual(km["status"], "active")
            self.assertTrue(km["analytics_connected"])
            self.assertEqual(km["connected_apps"], ["Mixpanel", "Sentry"])
            self.assertIsNotNone(km["primary_kpi"])
            self.assertEqual(km["primary_kpi"]["analytics_app"], "Mixpanel")
            self.assertIn("25% of completed Photo rounds", km["primary_kpi"]["tied_to_success_criterion"])
            self.assertTrue(km["guardrail_kpis"], "Must include guardrail / counter-KPI metric alongside primary KPI")
            self.assertEqual(km["guardrail_kpis"][0]["direction"], "down")
            self.assertEqual(km["guardrail_kpis"][0]["analytics_app"], "Sentry")
            pass_b_text = living_spec.pass_b_context(spec_3, {})
            self.assertIn("Feature Success Criteria (Explicit or Implied):", pass_b_text)
            self.assertIn("**Guardrail / Counter-KPI**:", pass_b_text)


class PrototypeFirstExperienceLoopTests(unittest.TestCase):
    """Tests for the Prototype-First Progressive Specification ('Build -> Experience -> Refine') upgrades:
    1) TTFE Risk Engine re-tuning (reversible product hypotheses vs unsafe/irreversible blockers)
    2) Human Experience Loop (v0.1 -> v0.2 -> v0.3) and 5-way Change-Impact Classifier
    3) Architectural Decay / Cruft & Refactor Guardrail after >= 3 patches to the same file
    4) Safe Undo reverting git commits and restoring prior LivingFeatureSpec snapshot
    5) Intent-Convergence Metrics (TTFE, Time per Iteration, Iterations to Acceptance, Rework Rate, Interaction Burden)
    """

    def test_prototype_first_mode_acts_by_default_on_scoring_hypotheses_but_blocks_on_privacy(self):
        # Reversible scoring/tie-breaker rule with a proposed default acts by default in prototype_first mode
        scoring_spec = living_spec.normalize({
            "pass": "spec",
            "title": "[Leaderboard] Photo Mode",
            "intent": "Add a global leaderboard for Photo mode",
            "decisions": [{
                "question": "How should ties be resolved and do retries count toward ranking?",
                "proposed_default": "Highest score wins; retries allowed in v0.1; earliest timestamp breaks ties",
                "confidence": "low",
                "impact": "high",
                "reversibility": "easy",
                "resolvable_by": "human_decision",
            }],
        }, raw_input="Add a global leaderboard for Photo mode")
        res = risk_engine.evaluate(scoring_spec, prototype_first=True)
        self.assertTrue(res["intent_gate_passed"], "Reversible scoring hypothesis should not block v0.1 TTFE")
        self.assertEqual(res["decisions"][0]["action"], "assume_hypothesis")
        self.assertEqual(len(scoring_spec["product_hypotheses"]), 1)
        self.assertIn("retries allowed in v0.1", scoring_spec["product_hypotheses"][0]["chosen_default"])

        # Unsafe privacy/PII decision MUST still block even in prototype_first mode
        privacy_spec = living_spec.normalize({
            "pass": "spec",
            "title": "[Leaderboard] Photo Mode",
            "intent": "Add a global leaderboard for Photo mode",
            "decisions": [{
                "question": "Should the leaderboard expose private account email addresses?",
                "proposed_default": "Show display name only",
                "confidence": "low",
                "impact": "low",
                "reversibility": "easy",
                "resolvable_by": "default_assumption",
            }],
        }, raw_input="Add a global leaderboard for Photo mode")
        res_priv = risk_engine.evaluate(privacy_spec, prototype_first=True)
        self.assertFalse(res_priv["intent_gate_passed"], "Privacy/PII risk must always pause at Intent Gate")
        self.assertEqual(res_priv["behavior"], "clarify")

    def test_change_impact_classifier_covers_all_five_feedback_types(self):
        self.assertEqual(
            experience_loop.classify_feedback("Make the header font smaller and adjust button padding")["feedback_type"],
            "visual_or_copy",
        )
        self.assertEqual(
            experience_loop.classify_feedback("This shouldn't count retries. Also normalize scores so harder puzzles aren't penalized.")["feedback_type"],
            "behavior_change",
        )
        self.assertEqual(
            experience_loop.classify_feedback("This should also work for Abstract mode")["feedback_type"],
            "new_capability",
        )
        self.assertEqual(
            experience_loop.classify_feedback("Scrap this approach and redesign from scratch with a new architecture")["feedback_type"],
            "fundamental_rethink",
        )
        self.assertEqual(
            experience_loop.classify_feedback("Undo that last change")["feedback_type"],
            "undo",
        )

    def test_experience_loop_evolves_spec_locks_behaviors_triggers_refactor_guardrail_and_undoes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / ".orchestrator"
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "scoring.py").write_text("def score(): return 10\n")
            subprocess.run(["git", "add", "scoring.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "v0.1 initial"], cwd=root, capture_output=True, check=True)
            v1_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

            spec = living_spec.normalize({
                "pass": "spec",
                "title": "[Leaderboard] Photo Mode",
                "intent": "Add a global leaderboard for Photo mode",
                "acceptance_criteria": [
                    {"text": "Displays player names and scores sorted descending", "provenance": "explicit", "source": "user_prompt"},
                    {"text": "Retries count toward leaderboard ranking", "provenance": "inferred", "source": "prototype_hypothesis"},
                ],
                "assumptions": [
                    {"text": "Retries count toward leaderboard ranking", "provenance": "inferred", "source": "prototype_hypothesis"},
                ],
            }, raw_input="Add a global leaderboard for Photo mode", root=root)
            job = {
                "job_id": "20261118-feature-lb",
                "title": "[Leaderboard] Photo Mode",
                "status": "review-needed",
                "created_at": "2026-11-18T20:00:00+00:00",
                "ai_modified_files": ["scoring.py"],
                "spec": spec,
                "plan": {"summary": "Photo leaderboard", "likely_files": ["scoring.py"], "tasks": [{"title": "Build leaderboard", "likely_files": ["scoring.py"]}]},
            }

            # 1) Record first playable experience (v0.1) -> computes TTFE (120s)
            experience_loop.record_first_experience(job, commit_sha=v1_sha, now_iso="2026-11-18T20:02:00+00:00")
            self.assertEqual(job["iteration_version"], "0.1")
            self.assertEqual(job["ttfe_seconds"], 120.0)

            # 2) User reacts to v0.1: "This shouldn't count retries. Also normalize scores." -> v0.2 behavior_change
            out_v2 = experience_loop.apply_experience_feedback(
                job,
                "This shouldn't count retries. Also normalize scores so harder puzzles aren't penalized.",
                root=root,
                runtime_dir=runtime,
                now_iso="2026-11-18T20:03:00+00:00",
            )
            self.assertEqual(out_v2["version"], "0.2")
            self.assertEqual(out_v2["classification"]["feedback_type"], "behavior_change")
            self.assertEqual(out_v2["next_execution"], "build_refinement")
            ac_texts = [a["text"] for a in job["spec"]["acceptance_criteria"]]
            # Old inferred "Retries count toward leaderboard ranking" is superseded; explicit criterion is locked
            self.assertNotIn("Retries count toward leaderboard ranking", ac_texts)
            self.assertTrue(any("shouldn't count retries" in a.lower() for a in ac_texts))
            self.assertTrue(any("Displays player names and scores" in b["text"] for b in job["spec"]["locked_behaviors"]))

            # Commit v0.2 change in git so we can test Undo and Churn
            (root / "scoring.py").write_text("def score(first_attempt=True): return 10 if first_attempt else 0\n")
            subprocess.run(["git", "add", "scoring.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "v0.2 first attempt only"], cwd=root, capture_output=True, check=True)
            v2_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            job["experience_versions"][-1]["commit"] = v2_sha

            # 3) Third patch on scoring.py triggers the Architectural Churn / Refactor Guardrail (v0.3)
            out_v3 = experience_loop.apply_experience_feedback(
                job,
                "Adjust score multiplier calculation for time bonus",
                root=root,
                runtime_dir=runtime,
                now_iso="2026-11-18T20:04:00+00:00",
            )
            self.assertEqual(out_v3["version"], "0.3")
            self.assertTrue(out_v3["refactor_recommended"], "3rd patch to scoring.py must recommend refactoring")
            self.assertIn("scoring.py", job["churn_guardrail"]["hot_files"])
            prompt_v3 = experience_loop.format_refinement_prompt(job, "Adjust score multiplier", out_v3)
            self.assertIn("ARCHITECTURAL REFACTOR GUARDRAIL", prompt_v3)
            self.assertIn("LOCKED BEHAVIORS FROM PREVIOUS ITERATIONS", prompt_v3)

            # 4) User asks for a new capability ("This should also work for Abstract mode") -> creates backlog slice (v0.4)
            out_v4 = experience_loop.apply_experience_feedback(
                job,
                "This should also work for Abstract mode",
                root=root,
                runtime_dir=runtime,
                now_iso="2026-11-18T20:05:00+00:00",
            )
            self.assertEqual(out_v4["classification"]["feedback_type"], "new_capability")
            self.assertEqual(out_v4["next_execution"], "backlog_slice_created")
            self.assertTrue(out_v4["created_backlog_jobs"])

            # 5) User says "Undo that last change" -> reverts to previous spec snapshot (v0.5)
            out_v5 = experience_loop.apply_experience_feedback(
                job,
                "Undo that last change",
                root=root,
                runtime_dir=runtime,
                now_iso="2026-11-18T20:06:00+00:00",
            )
            self.assertEqual(out_v5["classification"]["feedback_type"], "undo")
            self.assertEqual(out_v5["next_execution"], "reverted")

            # 6) Verify Intent-Convergence Metrics calculation
            job["status"] = "completed"
            metrics = experience_loop.compute_convergence_metrics([job])
            self.assertEqual(metrics["avg_ttfe_seconds"], 120.0)
            self.assertEqual(metrics["avg_iteration_seconds"], 60.0)
            self.assertEqual(metrics["avg_iterations_to_accept"], 5.0)
            self.assertGreater(metrics["rework_regression_rate"], 0.0)


class ExperienceApiAndUiTests(unittest.TestCase):
    """End-to-end API, CLI action, and UI rendering tests for the Prototype-First Experience Loop."""

    def test_server_experience_endpoint_and_analytics_convergence_metrics(self):
        from orchestrator.web import server as ui

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            jdir = ui.jobs_dir(root)
            jdir.mkdir(parents=True, exist_ok=True)
            job_id = "20261118-feature-photo-lb"
            job_path = jdir / f"{job_id}.json"

            spec = living_spec.normalize({
                "pass": "spec",
                "title": "[Leaderboard] Photo Mode",
                "intent": "Add a global leaderboard for Photo mode",
                "acceptance_criteria": [
                    {"text": "Shows top 50 scores", "provenance": "explicit", "source": "user_prompt"},
                    {"text": "Counts retries toward ranking", "provenance": "inferred", "source": "prototype_hypothesis"},
                ],
            }, raw_input="Add a global leaderboard for Photo mode", root=root)
            job = {
                "job_id": job_id,
                "type": "feature-plan",
                "title": "[Leaderboard] Photo Mode",
                "status": "review-needed",
                "approved": True,
                "auto_passed_gates": True,
                "created_at": "2026-11-18T20:00:00+00:00",
                "ai_modified_files": ["wordgame/scoring.py"],
                "spec": spec,
                "plan": {
                    "summary": "Photo mode leaderboard",
                    "likely_files": ["wordgame/scoring.py"],
                    "tasks": [{"title": "Build leaderboard", "likely_files": ["wordgame/scoring.py"]}],
                },
            }
            experience_loop.record_first_experience(job, commit_sha="abc1234", now_iso="2026-11-18T20:01:30+00:00")
            ui.write_json_file(job_path, job)

            # 1) job_detail exposes experience workspace state and spec
            detail = ui.job_detail(root, job_id)
            self.assertEqual(detail["experience"]["version"], "0.1")
            self.assertTrue(detail["experience"]["preview_ready"])
            self.assertEqual(detail["experience"]["ttfe_seconds"], 90.0)
            self.assertEqual(detail["summary"]["iteration_version"], "0.1")

            # 2) Run job_actions.refine for new_capability and undo
            from orchestrator.scripts import job_actions
            rc_cap = job_actions.refine(job_path, "This should also work for Abstract mode")
            self.assertEqual(rc_cap, 0)
            after_cap = ui.read_json_file(job_path)
            self.assertEqual(after_cap["iteration_version"], "0.2")
            self.assertTrue(after_cap.get("deferred_job_ids"))

            rc_undo = job_actions.refine(job_path, "Undo that last change")
            self.assertEqual(rc_undo, 0)
            after_undo = ui.read_json_file(job_path)
            self.assertEqual(after_undo["iteration_version"], "0.3")
            self.assertEqual(after_undo["experience_versions"][-1]["feedback_type"], "undo")

            # 3) Mark completed and verify analytics_overview -> performance includes all 5 Intent-Convergence Metrics
            after_undo["status"] = "completed"
            ui.write_json_file(job_path, after_undo)
            overview = ui.analytics_overview(root)
            perf = overview["performance"]
            self.assertEqual(perf["avg_ttfe_seconds"], 90.0)
            self.assertEqual(perf["avg_iterations_to_accept"], 3.0)
            self.assertIsNotNone(perf["rework_regression_rate"])
            self.assertEqual(perf["avg_interaction_burden"], 0.0)

    def test_ui_includes_experience_workspace_and_convergence_metrics(self):
        app_js = (ROOT_DIR / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        for expected in (
            'class="job-stepper-history"',
            "Version history (",
            "data-experience-undo",
            "data-experience-chip",
            "Architectural refactor guardrail active",
            "data-run-experience-version",
            "data-run-feedback-impact",
            "Time to first experience (TTFE)",
            "Time per feedback iteration",
            "Iterations to acceptance",
            "Rework &amp; regression rate",
            "User interaction burden",
        ):
            self.assertIn(expected, app_js)
        self.assertNotIn('id="experience-workspace"', app_js)
        self.assertNotIn("Experience &amp; Refine · Version", app_js)
        self.assertNotIn("continue setup", app_js)


class PipelineDebloatingTests(unittest.TestCase):
    """Tests for the 6 LLM prompt & step de-bloating optimizations."""

    def test_conditional_architect_verifier_skips_clean_grounded_high_tier_plans(self):
        from types import SimpleNamespace
        spec = living_spec.normalize({
            "pass": "combined",
            "title": "[Scoring] Bonus badge",
            "intent": "Add bonus badge",
            "stories": [{"id": "story-1", "title": "Bonus badge", "summary": "Show bonus badge"}],
        }, raw_input="Add bonus badge")
        risk = risk_engine.evaluate(spec)
        grounded_plan = {
            "tasks": [{
                "title": "Render bonus badge when score bonus applies",
                "description": "Add badge to score view",
                "acceptance_criteria": ["Badge renders when bonus > 0"],
                "likely_files": ["wordgame/scoring.py", "tests/test_scoring.py"],
                "tests": ["test_bonus_badge"],
                "complexity": "small",
            }],
        }
        high_paid = SimpleNamespace(tier=2, cost_factor=1.0)
        free_model = SimpleNamespace(tier=3, cost_factor=0.0)
        extreme_model = SimpleNamespace(tier=1, cost_factor=2.0)

        # Clean, grounded, single-slice plan from High-tier paid model skips redundant verifier call
        self.assertFalse(risk_engine.should_run_architect_verifier(spec, risk, grounded_plan, high_paid))
        # Extreme model always skips
        self.assertFalse(risk_engine.should_run_architect_verifier(spec, risk, grounded_plan, extreme_model))
        # Free model still runs verifier
        self.assertTrue(risk_engine.should_run_architect_verifier(spec, risk, grounded_plan, free_model))
        # Ungrounded plan (missing likely_files/tests) still runs verifier
        ungrounded_plan = {"tasks": [{"title": "Task 1", "acceptance_criteria": ["AC"], "likely_files": [], "tests": []}]}
        self.assertTrue(risk_engine.should_run_architect_verifier(spec, risk, ungrounded_plan, high_paid))

    def test_single_pass_collapse_and_combined_quality_retry_minimize_planner_calls(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_tmp:
            root = Path(tmp)
            old_env = os.environ.copy()
            old_modules = dict(sys.modules)
            try:
                os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(root)
                os.environ["ORCHESTRATOR_RUNTIME_DIR"] = ".orchestrator"
                os.environ["ORCHESTRATOR_USER_STATE_DIR"] = state_tmp
                subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
                (root / "README.md").write_text("# Test")
                for mod in [m for m in sys.modules if m.startswith("orchestrator") or m in ("common", "new_job")]:
                    del sys.modules[mod]
                from orchestrator import cli
                cli.main(["init", "--root", str(root), "--project-name", "Test", "--base-branch", "main"])
                for mod in [m for m in sys.modules if m.startswith("orchestrator") or m in ("common", "new_job")]:
                    del sys.modules[mod]

                from orchestrator.scripts import common, new_job

                def make_paths(job_id: str):
                    jdir = root / ".orchestrator" / "jobs"
                    odir = root / ".orchestrator" / "output" / job_id
                    jdir.mkdir(parents=True, exist_ok=True)
                    odir.mkdir(parents=True, exist_ok=True)
                    return common.JobPaths(job_file=jdir / f"{job_id}.json", brief_file=odir / "brief.md", output_dir=odir)

                # 1) Single-Pass Collapse ("pass": "combined" with tasks & test_cases) completes in 1 LLM call (Pass B skipped)
                single_pass_calls: list[str] = []

                def fake_single_pass(model, prompt, *a, **k):
                    single_pass_calls.append(prompt)
                    return (json.dumps({
                        "pass": "combined",
                        "title": "[Scoring] Long-word bonus",
                        "summary": "Add 10-point bonus",
                        "intent": "Add 10-point bonus for 7+ letter words",
                        "acceptance_criteria": [{"text": "7+ letters add 10 points", "provenance": "explicit", "source": "user_prompt"}],
                        "tasks": [{
                            "title": "A 7-letter word scores 10 extra points end to end",
                            "description": "Add bonus to score()",
                            "acceptance_criteria": ["7+ letters add 10 points"],
                            "likely_files": ["README.md"],
                            "tests": ["test_bonus"],
                            "complexity": "small",
                        }],
                        "test_cases": [{
                            "title": "Seven letters get bonus",
                            "type": "unit",
                            "expected": "10 extra points",
                            "covers": ["7+ letters add 10 points"],
                            "tests": ["test_bonus"],
                        }],
                    }), "mock-model", "sid-1")

                with patch.object(new_job, "ROOT", root), \
                     patch.object(new_job, "make_job_paths", side_effect=make_paths), \
                     patch.object(new_job, "run_llm", side_effect=fake_single_pass), \
                     patch("sys.stdin", io.StringIO("")):
                    new_job.main(["feature", "--summary", "Add 10-point bonus", "--branch-mode", "manual",
                                  "--no-dispatch", "--allowed-models", "gemini", "--allowed-machines", "local"])
                self.assertEqual(len(single_pass_calls), 1, "Single-pass combined response must skip Pass B")
                for jf in (root / ".orchestrator" / "jobs").glob("*.json"):
                    jf.unlink()

                # 2) Combined quality retry: a plan with BOTH missing test cases AND layered tasks makes only 1 retry call
                combined_retry_calls: list[str] = []
                bad_then_good = [
                    json.dumps({
                        "pass": "combined",
                        "title": "Layered & Untested",
                        "summary": "Bad plan",
                        "tasks": [
                            {"title": "Database schema for scores", "description": "Schema", "acceptance_criteria": ["AC1"], "likely_files": ["README.md"], "tests": [], "complexity": "small"},
                            {"title": "Scores API endpoint", "description": "API", "acceptance_criteria": ["AC1"], "likely_files": ["README.md"], "tests": [], "complexity": "small"},
                            {"title": "Scores UI screen", "description": "UI", "acceptance_criteria": ["AC1"], "likely_files": ["README.md"], "tests": [], "complexity": "small"},
                        ],
                        "test_cases": [],  # missing test cases AND layered tasks!
                    }),
                    json.dumps({
                        "title": "Sliced & Tested",
                        "summary": "Fixed plan",
                        "tasks": [{
                            "title": "Player sees top scores end to end",
                            "description": "End to end slice",
                            "acceptance_criteria": ["AC1"],
                            "likely_files": ["README.md"],
                            "tests": ["test_ac1"],
                            "complexity": "small",
                        }],
                        "test_cases": [{
                            "title": "AC1 holds",
                            "type": "unit",
                            "expected": "Top scores render",
                            "covers": ["AC1"],
                            "tests": ["test_ac1"],
                        }],
                    }),
                ]

                def fake_combined_retry(model, prompt, *a, **k):
                    combined_retry_calls.append(prompt)
                    return (bad_then_good[min(len(combined_retry_calls) - 1, 1)], "mock-model", f"sid-{len(combined_retry_calls)}")

                with patch.object(new_job, "ROOT", root), \
                     patch.object(new_job, "make_job_paths", side_effect=make_paths), \
                     patch.object(new_job, "run_llm", side_effect=fake_combined_retry), \
                     patch("sys.stdin", io.StringIO("")):
                    new_job.main(["feature", "--summary", "Show top scores", "--branch-mode", "manual",
                                  "--no-dispatch", "--allowed-models", "gemini", "--allowed-machines", "local"])

                self.assertEqual(len(combined_retry_calls), 2, "Must consolidate test-case and slice retries into 1 retry call")
                self.assertIn("TEST CASES", combined_retry_calls[1])
                self.assertIn("PLAN SHAPE (re-plan requested)", combined_retry_calls[1])
            finally:
                sys.modules.clear()
                sys.modules.update(old_modules)
                os.environ.clear()
                os.environ.update(old_env)

    def test_cohesive_task_batching_combines_overlapping_small_tasks_and_separates_disjoint_tasks(self):
        from orchestrator.scripts import worker_run
        overlapping = [
            {"title": "Add bonus constant", "description": "Define constant", "acceptance_criteria": ["AC1"],
             "likely_files": ["wordgame/scoring.py", "tests/test_scoring.py"], "tests": ["test_1"], "complexity": "small"},
            {"title": "Apply bonus in score()", "description": "Update score()", "acceptance_criteria": ["AC2"],
             "likely_files": ["wordgame/scoring.py", "tests/test_scoring.py"], "tests": ["test_2"], "complexity": "small"},
        ]
        self.assertEqual(worker_run.can_batch_cohesive_tasks(overlapping, []), [0, 1])
        merged = worker_run.merge_batched_tasks(overlapping, [0, 1])
        self.assertIn("Add bonus constant + Apply bonus in score()", merged["title"])
        self.assertEqual(merged["acceptance_criteria"], ["AC1", "AC2"])
        self.assertEqual(merged["tests"], ["test_1", "test_2"])

        disjoint = [
            {"title": "Update scoring", "likely_files": ["wordgame/scoring.py"], "complexity": "small"},
            {"title": "Document in README", "likely_files": ["README.md"], "complexity": "small"},
        ]
        self.assertEqual(worker_run.can_batch_cohesive_tasks(disjoint, []), [])

    def test_deterministic_failure_summary_and_prd_skip_guardrails(self):
        from orchestrator.scripts import run_build_and_tests as runner
        summary = runner.analyze_failure("test", "Running suite...\nFAIL: test_score (tests.TestScoring)\nAssertionError: 10 != 20\n")
        self.assertIn("Deterministic Test Failure Summary", summary)
        self.assertIn("FAIL: test_score", summary)

        # prd.should_check skips visual_or_copy and undo refinements, and YOLO-only autoresponses on bug jobs
        self.assertFalse(prd.should_check({
            "type": "feature-plan",
            "experience_feedback": [{"feedback_type": "visual_or_copy"}],
        }))
        self.assertFalse(prd.should_check({
            "type": "feature-plan",
            "experience_feedback": [{"feedback_type": "undo"}],
        }))
        self.assertFalse(prd.should_check({
            "type": "bug-fix",
            "clarification_history": [{"question": "Q?", "answer": "A", "auto": True}],
        }))
        self.assertTrue(prd.should_check({
            "type": "bug-fix",
            "clarification_history": [{"question": "Q?", "answer": "A", "auto": False}],
        }))
        self.assertTrue(prd.should_check({"type": "feature-plan"}))


if __name__ == "__main__":
    unittest.main()
