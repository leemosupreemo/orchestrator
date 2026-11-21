# AI Workflow

Use `orchestrator console` from the repository root to create and manage jobs for swift-orchestrator.

Recommended flow:

1. Run `orchestrator check` after initial setup or toolchain changes.
2. Run `orchestrator check-config` after editing `.orchestrator/project.json` or machine config.
3. Create jobs from the console.
4. Review generated branches and pull requests before merging.
5. Keep generated `.orchestrator/jobs/`, `logs/`, `output/`, and `state/` files out of Git.

## Adaptive Specification & Planning Workflow

The default entry point in the web UI and `build_new_job` is **Freestyle ("Say it your way")**, which accepts anything from a short two-sentence request to a full PRD with acceptance criteria and file references.

### Lifecycle Stages & Live Status Container

While a planning or replanning run is active, the worker emits structured phase headers and `[spec]` / `[gate]` telemetry consumed by `RunProgress` (`orchestrator/web/run_progress.py`), `RunTimeline` (`orchestrator/web/run_timeline.py`), and the live status container (`runStatus` in `orchestrator/web/static/app.js`):

1. **Pass A · Spec synthesis (`Synthesizing feature spec`)**: Extracts requirements with provenance (`explicit`, `code_derived`, `inferred`), synthesizes explicit or implied `success_criteria`, ties them to a primary KPI and guardrail / counter-KPI metric when an analytics app is connected (or recommends KPI setup once and then ignores KPIs until analytics is connected in setup), checks for stale repository paths, and decomposes the request into vertical story slices.
2. **Pass A · Risk & ambiguity assessment (`Assessing ambiguity & risk`)**: Classifies open decisions by confidence, impact, and reversibility, and inspects the repository for technically investigable questions.
3. **Intent Gate (`Evaluating intent gate`)**:
   - Pauses with `Intent gate · conflicting requirements` when the request contradicts `prd.md` non-goals or approved `docs/architecture.json`.
   - Pauses with `Intent gate · waiting for clarification` when a high-impact or irreversible product decision requires human input.
   - Proceeds with `Intent gate · passed (<behavior>)` when all uncertainties are resolved or covered by documented low-risk defaults.
4. **Pass B · Execution planning (`Planning execution tasks`)**: Plans concrete builder tasks, `likely_files`, and automated `test_cases` for the ready story slice, deferring later slices into `plan.deferred`.
5. **Plan Gate (`Validating plan gate`)**:
   - Reports `Plan gate · auto-passed` when a small, low-risk slice passes `test_cases`, `slice_check`, `plan_size`, and verifier checks without touching manifest files.
   - Reports `Plan gate · ready for review` or `Plan gate · needs attention` when human review is required.
   - Reports `Plan gate · replan invariant violation` if a mid-execution replan attempts to drop an explicitly approved acceptance criterion or violate a non-goal.
6. **Rolling-Wave Follow-Up Slices (`Deferred slice · no plan yet`)**: Approving a job with deferred stories automatically creates waiting `backlog` jobs in the same feature so each subsequent slice is planned against fresh code after the earlier slice lands.
7. **Prototype-First Experience & Refinement Loop (`Build → Experience → Refine`)**:
   - Once the initial slice reaches `review-needed`, it is recorded as **Version `v0.1` (`Preview ready`)** with its **Time to First Experience (TTFE)** and clickable **Assumed Product Hypotheses**.
   - Submitting user feedback (`orchestrator script job_actions.py refine <job_file> --feedback "..."` or the Job page's **Experience & Refine** workspace) runs the Change-Impact Classifier (`Analyzing feedback impact`), evolves the `LivingFeatureSpec` (`Refining from user feedback` or `Consolidating & refactoring` when file churn $\ge 3$), or safely reverts (`Reverting last iteration`), streaming `[experience]` version and impact updates live to the status container.

### How planning assesses risk

The planner labels each open decision with its confidence, impact, reversibility, and the way it can be resolved. Confidence and impact are high or low. Reversibility is easy or costly. A decision can use a default, call for repository inspection, require a person's answer, or require resolution of conflicting requirements.

Orchestrator applies rules to the decision's question and proposed default. Privacy, authentication, billing, scoring fairness, and destructive data changes are high-impact signals. Schema migrations, persistent data formats, and public API changes signal costly reversibility. Cosmetic changes such as copy, spacing, and local naming generally stay low impact and easy to reverse when those other signals are absent.

The **Intent Gate** uses those labels to proceed with a documented default, look for repository evidence, or ask a targeted question. An unresolved conflict or a decision that still needs a person's answer pauses planning. A high-confidence decision can proceed to planning with a defensible default. The **Plan Gate** still checks the plan, and high impact or costly reversibility rules out automatic approval.

Open **Plan notes** on the job page to see high, medium, and low risks in that order. A costly-to-reverse decision appears under **High risk**. **Medium risk** appears when a saved note explicitly records that level. Free-text risks without a rating appear under **Unrated risks**. Unrated assumptions and constraints have their own groups. Multi-sentence assumptions and risks show a short first line that expands to reveal the remaining text and rationale. Earlier job records with duplicated notes or dictionary-shaped text are cleaned up when displayed. Recorded low-risk defaults keep their label when an older shared job carries only the flattened assumption list.

## Model choice within a feature job

Feature plans label each task `small`, `medium`, or `large`. The worker keeps the selected builder for medium and large tasks. For a small, localized task (for example, changing spacing or positioning), it prefers an enabled free or local coding model, then the least-cost enabled coding model. It only considers models allowed for that job; if none qualify, it uses the selected builder. If a cheaper model fails, the normal model fallback can take over.

### Model outcomes and shadow recommendations

The job JSON keeps a `model_outcomes` history with the selected and actual model IDs, role, job type, timestamp, and result. Builder entries include task numbers for feature tasks or batches. A planner entry means the plan passed its gate. A builder entry means the worker accepted the changes after its build and test checks. A reviewer entry means a review was generated. Job sharing carries these outcomes through the project's Git state ref when enabled. The account snapshot omits them.

The worker writes model attempts and shadow comparisons to `.orchestrator/logs/model-attempts.jsonl` in the project runtime directory. The attempt records include the requested model, model tried, elapsed time, any format retry, and result. Results distinguish an accepted response, invalid JSON after a format retry, a timeout, quota, sign-in failure, model unavailability, and other execution errors. A `call_id` links attempts to their shadow comparison. The log stays on the computer that ran the call and contains metadata without prompts, replies, or raw errors.

Shadow comparisons show the recommended model, the model that answered (or `null` if all attempts failed), and the evidence used. The recommendation considers locally runnable candidates from the initial model list. It scores accepted responses and final JSON-format failures from recent calls with the same role and job type. Other failures stay outside that score. It uses the first runnable candidate when that candidate lacks three comparable results. With enough evidence, it compares candidates with at least three results and uses the original order for ties. Live routing continues to use the existing model selector. The shadow score measures response acceptance. The job's outcome history records validation at later stages.

## LLM Call & Prompt Efficiency Guardrails

To keep jobs fast and avoid prompt bloat without sacrificing quality:

1. **Adaptive Single-Pass Collapse (`"pass": "combined"`)**: When a feature request fits in a single vertical slice (`stories` has 1 story, `deferred` is empty, $\le 3$ tasks) with no blocking ambiguity, Pass A synthesizes both the `LivingFeatureSpec` and the execution `tasks` + `test_cases` in one response, skipping the separate Pass B LLM call.
2. **Consolidated Plan-Quality Retry (`enforce_plan_quality`)**: Deterministic checks for test-case coverage (`test_cases.plan_problems`), job size budget (`plan_size.measure`), and vertical slicing (`slice_check.problems`) run together and trigger at most **1 combined self-correction LLM call** instead of up to 4 sequential retries.
3. **Conditional Senior Architect Verifier (`should_run_architect_verifier`)**: When a primary high-tier planner produces a grounded single-slice plan that auto-passes the deterministic Plan Gate, the extra `verifier.md` LLM call is skipped (still running for free/medium-tier models, multi-story epics, manifest changes, or ungrounded plans).
4. **Cohesive Task Batching (`can_batch_cohesive_tasks`)**: When 2–3 small remaining tasks share overlapping `likely_files` ($\le 4$ total unique files), `worker_run.py` executes them in a single combined builder pass while keeping disjoint-file tasks separate for per-task commit revert.
5. **Single-Pass Build Capture & Deterministic Failure Extraction**: `run_build_and_tests.py` captures build output on the first execution (never running failed builds twice) and summarizes compiler/test failures deterministically (`extract_deterministic_failure_summary`) so the autonomous debug loop doesn't burn an extra summary LLM call before `debug_job.py` inspects the logs.
6. **Trimmed Builder Prompts & Smart PRD Sync**: `run_builder.py` only injects the UI screenshot capture block when a task touches frontend/UI files or `ui_review` is configured; `generate_test_index.py` and `test_cases.library_index` cap and relevance-filter large test indexes; and `prd.should_check` skips post-job PRD update LLM calls for `visual_or_copy` / `undo` refinements and YOLO-only autoresponses on non-feature jobs.
