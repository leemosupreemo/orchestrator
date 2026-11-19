# Architecture

The Swift Orchestrator is a Python-based multi-agent system designed to automate the development lifecycle of Swift and Xcode projects. It coordinates various AI agents to perform tasks ranging from planning and design to implementation, testing, and delivery.

## System Overview

The orchestrator operates as a standalone CLI tool that can be integrated into any Swift repository. It manages state via local job files and leverages external AI providers for cognitive tasks.

### Key Components

1.  **CLI Entry Point (`orchestrator/cli.py`)**: The primary interface for users to interact with the orchestrator.
2.  **Project Config (`orchestrator/project_config.py`)**: Manages project-specific settings, Xcode configurations, and worker definitions.
3.  **Adaptive Specification & Planning Pipeline (`orchestrator/living_spec.py`, `orchestrator/risk_engine.py`, `orchestrator/scripts/new_job.py`)**: Normalizes everything from a two-sentence prompt to a full PRD into a versioned **Living Feature Spec**, evaluates uncertainty and risk deterministically, and plans in two passes.
4.  **Agent Loop (`orchestrator/scripts/agent_loop.py`)**: The core execution engine that sequences agent tasks.
5.  **LLM Integration (`orchestrator/scripts/llm.py`)**: Provides a unified interface to multiple AI providers (Antigravity, Claude, OpenAI, Ollama).
6.  **Job Management**: State is persisted in `.orchestrator/jobs/` as JSON files, allowing for asynchronous execution and recovery. How a job stops, recovers, is backed up and moves between computers: [Job recovery and ownership](job-recovery.md).
7.  **Live Run Status & Timeline (`orchestrator/web/run_progress.py`, `orchestrator/web/run_timeline.py`)**: Streams worker phases, adaptive planning stages (`[spec]`), gate checkpoints (`[gate]`), and active model calls into the web UI's live status container and step timeline.

## Adaptive Specification & Planning Architecture

Every feature or freestyle request passes through four logical stages before implementation begins:

1.  **Input Interpreter & Context Analyzer (`orchestrator/living_spec.py`)**:
    *   Accepts sparse text, structured briefs, attached spec files/URLs, product documents (`prd.md`), and approved architecture (`docs/architecture.json`).
    *   Measures decision-completeness depth (`sparse`, `partial`, `detailed`) based on resolved architectural and product signals rather than raw text length.
    *   Verifies referenced file paths against the repository (`verify_evidence_paths`), flagging stale or renamed paths and suggesting real repository replacements.
2.  **Spec & Decision Engine (`orchestrator/living_spec.py` + `orchestrator/risk_engine.py`)**:
    *   Synthesizes the canonical **Living Feature Spec** (persisted to `job["spec"]` and `.orchestrator/output/<job_id>/spec.json` & `spec.md`) with explicit provenance tags (`explicit`, `code_derived`, `inferred`) and source references.
    *   Ensures every feature carries **Success Criteria** (`spec["success_criteria"]`, explicit or implied from context/expected outcome). When an analytics or monitoring app (Mixpanel, Amplitude, PostHog, Sentry, Datadog, New Relic) is connected, `spec["kpi_metrics"]` ties those success criteria to a **Primary KPI** and at least one **Guardrail / Counter-KPI** metric. When no analytics app is connected, it recommends setting up analytics/KPIs **once** (`kpi_setup_recommended_once` in `.orchestrator/config/settings.json`) and then stays silent on KPIs until analytics is connected in setup.
    *   Evaluates open decisions across qualitative **Confidence** (`high`/`medium`/`low`), **Impact** (`low`/`medium`/`high`), and **Reversibility** (`easy`/`moderate`/`costly`) with deterministic guardrails for privacy/PII, security/auth, scoring/ranking fairness, schema migrations, and manifest files:
        *   *High confidence, low risk* → `proceed` automatically.
        *   *Low confidence, low impact, reversible* → `assume_default` and record in `spec["assumptions"]`.
        *   *Low confidence, technically investigable* → `investigate` the repository for matching symbols/files and resolve automatically when found.
        *   *Low confidence, high product impact or costly reversibility* → `clarify` (pause at the **Intent Gate** with a targeted human question).
        *   *Conflicting explicit requirements* (e.g., request contradicts `prd.md` `"Not this"` or approved `docs/architecture.json`) → `stop_conflict` at the **Intent Gate**.
3.  **Adaptive Dual-Pass / Single-Pass Rolling-Wave Planner (`orchestrator/prompts/planner_spec.md` + `orchestrator/prompts/planner_feature.md`)**:
    *   **Pass A (Outcome, Story Decomposition & Single-Pass Collapse)**: Synthesizes the Living Feature Spec, decomposes the epic into vertical user-story slices (`MVP` / `Next` / `Later`), selects the first `ready_story_id`, and evaluates the **Intent Gate**. For single-slice requests ($\le 3$ tasks, 1 story, no `deferred` stories, no blocking ambiguity), Pass A emits `"pass": "combined"` with `tasks` and `test_cases` in one shot—skipping the separate Pass B LLM call. If the Intent Gate pauses for clarification or conflict resolution, Pass B is skipped until resolved.
    *   **Pass B (Repository-Aware Execution Planning)**: Runs for multi-story epics or post-clarification plans; deeply plans implementation tasks, affected files, and automated `test_cases` for only the ready story slice (omitting redundant `project_docs` already distilled in `pass_b_context`), while moving remaining stories into `plan["deferred"]`.
    *   **Rolling-Wave Deferred Backlog**: When a job with `deferred` slices is approved (or auto-approved), `living_spec.create_deferred_backlog_jobs` automatically creates waiting `status: "backlog"` follow-up jobs linked via `deferred_from` and `feature`.
4.  **Lightweight Gates, Single-Retry Validation & Mid-Execution Replan Protection (`orchestrator/risk_engine.py`, `orchestrator/scripts/new_job.py`)**:
    *   **Consolidated Plan-Quality Retry (`enforce_plan_quality`)**: Runs `test_cases.plan_problems`, `plan_size.measure`, and `slice_check.problems` together and performs at most **1 combined self-correction LLM call** (instead of up to 4 sequential retries).
    *   **Conditional Senior Architect Verifier (`should_run_architect_verifier`)**: Skips the extra `verifier.md` LLM call when a high-tier paid planner produces a grounded (`likely_files` + `tests`), single-slice plan that already auto-passes the deterministic Plan Gate; retains verification for free/medium-tier models, multi-story epics, manifest edits, and ungrounded plans.
    *   **Intent Gate**: Blocks on conflicts or high-impact ambiguity; auto-passes when decisions are explicit, code-derived, or low-risk defaults.
    *   **Plan Gate**: Evaluates `test_cases`, `slice_check`, `plan_size`, manifest modifications, and Senior Architect verification. Small, low-risk, warning-free slices automatically pass (`auto_pass: true`, `job["auto_passed_gates"] = true`).
    *   **Mid-Execution Replan Invariants**: During mid-run replanning, completed tasks are preserved and `living_spec.validate_replan_invariants` blocks silent removal of `[explicit]` acceptance criteria or violation of `[explicit]` non-goals.
5.  **Prototype-First Human Experience Loop (`orchestrator/experience_loop.py`)**:
    *   Separates the **Autonomous Engineering Loop** (`debug_job.py`, triggered by build/test/review failures) from the **Human Experience Loop** (`experience_loop.py`, triggered when the user tries a working version `v0.1`, `v0.2`, ... and reacts to it).
    *   Optimizes **Time to First Experience (TTFE)** by treating reversible product-behavior defaults (such as initial scoring or tie-breaker rules) as v0.1 **Product Hypotheses** (`spec["product_hypotheses"]`) while keeping unsafe/irreversible choices (privacy/PII, auth, billing, destructive migrations) gated.
    *   Runs a 5-way **Change-Impact Classifier** (`visual_or_copy`, `behavior_change`, `new_capability`, `fundamental_rethink`, `undo`), locks previously accepted criteria against regression (`spec["locked_behaviors"]`), and enforces an **Architectural Churn / Refactor Guardrail** when files are patched $\ge 3$ times across iterations.
    *   Computes the 5 **Intent-Convergence Metrics** (`avg_ttfe_seconds`, `avg_iteration_seconds`, `avg_iterations_to_accept`, `rework_regression_rate`, `avg_interaction_burden`) exposed on the Analytics page.
6.  **Execution & Prompt Efficiency Guardrails (`worker_run.py`, `run_builder.py`, `run_build_and_tests.py`, `prd.py`)**:
    *   **Cohesive Task Batching (`can_batch_cohesive_tasks`)**: Executes 2–3 explicitly small remaining tasks that share overlapping `likely_files` ($\le 4$ total unique files) in a single combined builder pass while keeping disjoint-file tasks in separate per-task commits for independent revert.
    *   **Single-Pass Build Capture & Deterministic Failure Summaries**: `run_build_and_tests.py` captures build output on the first execution (never re-running failed builds) and summarizes compiler/test failures deterministically (`extract_deterministic_failure_summary`) without burning an extra summary LLM call before `debug_job.py` inspects the logs; `debug_job.py` omits `build.log` when the build succeeded.
    *   **Trimmed Context & Smart PRD Sync**: `run_builder.py` injects `### UI screenshot evidence` only when `likely_files` touches frontend/UI files or `ui_review` is configured; `generate_test_index.py` (max 35 suites) and `test_cases.library_index` (max 40 relevance-filtered cases) bound test-index prompt size; and `prd.should_check` skips post-job PRD LLM checks on `visual_or_copy` / `undo` refinements and YOLO-only autoresponses on non-feature jobs.

## Multi-Agent Workflow

The system uses a tiered agent architecture:

*   **Planner**: Analyzes requirements and breaks them down into actionable tasks.
*   **Designer**: Creates high-fidelity specs for features (Stitch AI mode).
*   **Builder**: Executes individual tasks, writing code and tests.
*   **Reviewer**: Analyzes code changes for quality and adherence to standards.
*   **Verifier**: Validates that the implementation meets the original requirements.
*   **Debugger**: Specialized agent for root-cause analysis and fixing failures.

## Execution Modes

*   **Local**: Executes tasks directly on the host machine.
*   **SSH (Fleet)**: Dispatches tasks to a fleet of remote macOS workers, enabling parallel builds and testing across different environments.

## Integration Points

*   **Xcode**: Interfaces with `xcodebuild`, `xcrun`, and `simctl`.
*   **GitHub**: Uses `gh` CLI for issue tracking, branch management, and pull requests.
*   **Firebase**: Automates app distribution for visual QA.
