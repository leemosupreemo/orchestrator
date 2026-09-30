# Capability-Aware Agentic Execution

## Summary

Orchestrator remains the top-level authority for user intent, planning, permissions, scheduling, handoffs, and QA. Models that can operate agentically through their current execution adapter may act as short-lived sub-agents for bounded work packages. Models without that capability remain useful through guided or advisory execution.

Agentic capability belongs to the complete execution route—model, adapter, machine, tools, authentication, and permissions—not to a model name alone. Orchestrator selects the strongest safe route automatically and degrades gracefully when an agentic route becomes unavailable.

## Execution Modes

- **Advisory:** Produces analysis or structured output without changing the workspace.
- **Guided:** Produces implementation guidance or code while Orchestrator owns edits, commands, tests, and iteration.
- **Agentic:** May inspect the workspace, edit files, run commands and tests, and self-correct within a bounded assignment.

Execution mode is internal by default. Advanced controls may constrain or override it, but normal users choose a development approach rather than managing model mechanics.

## Authority Model

Orchestrator owns:

- The shared Intent Brief and acceptance criteria
- Work decomposition and dependency ordering
- Role assignment and execution-route selection
- Permission, tool, time, and iteration budgets
- File/resource reservations and concurrency
- Handoff validation, retries, rerouting, and fallback
- Independent QA and final acceptance

An agentic sub-agent owns only its assigned work package. It may not broaden scope, weaken acceptance criteria, change permissions, or recursively delegate unless Orchestrator explicitly authorizes it.

## Execution Profiles

An execution profile describes a model as it is available through a specific adapter and machine:

```json
{
  "model": "gpt-5.5",
  "adapter": "codex",
  "machine": "local",
  "execution_modes": ["advisory", "guided", "agentic"],
  "tools": ["read", "edit", "shell", "tests"],
  "supports_sessions": true,
  "supports_resume": true,
  "supports_structured_handoff": true,
  "max_parallel_tasks": 2,
  "verified_at": "..."
}
```

Profiles combine static adapter metadata with runtime availability. Static metadata describes the adapter's intended abilities. Runtime discovery confirms that its CLI, model, authentication, workspace, and tools are currently usable. Probe results are cached and invalidated when the adapter, model, machine, or authentication state changes.

## Work Packages

Planning produces bounded role-tagged work packages rather than permanent role processes:

```json
{
  "id": "implement-profile-migration",
  "role": "database_specialist",
  "objective": "Implement and verify the profile migration",
  "required_mode": "agentic",
  "required_tools": ["read", "edit", "shell", "tests"],
  "dependencies": ["schema-design"],
  "likely_files": ["database/", "tests/"],
  "acceptance_criteria": ["Existing profiles survive migration"],
  "budgets": {"iterations": 4, "minutes": 20}
}
```

Roles provide the required engineering perspective. A sub-agent is created for one work package and terminates after submitting its handoff.

## Routing

The resolver filters routes by user-allowed models and machines, required tools, permissions, and current availability. It then scores mode fit, role suitability, quality, context, cost, and speed. Agentic suitability receives strong weight for implementation and debugging; reasoning and context retain higher importance for architecture and verification.

Explicit model choices remain constraints. They do not falsely imply agentic operation: if the selected model's current adapter cannot act agentically, Orchestrator uses guided execution or another allowed route according to fallback policy.

## Lifecycle and Handoffs

Each package moves through:

```text
queued → matched → running → handoff-submitted → validating
       → accepted | retrying | rerouted | blocked
```

Every sub-agent receives the Intent Brief, one role, one objective, relevant prior handoffs, explicit tool and file scope, dependencies, acceptance criteria, budgets, and stop conditions.

Every completion must report:

- Work performed and decisions made
- Evidence and commands executed
- Files changed
- Tests and acceptance-criteria status
- Remaining risks or uncertainty
- Recommended next action

Orchestrator checks the report against actual workspace changes and test results. A worker cannot finally accept its own work.

## Parallelism and Conflict Control

Independent packages may run concurrently only when their dependencies are satisfied, likely file/resource scopes do not overlap, and adapter/machine limits allow it. Orchestrator reserves declared scopes before dispatch. Unexpected overlap causes the later package to be serialized or rerouted rather than merging competing changes blindly.

The first implementation uses conservative concurrency and treats unknown scope as conflicting.

## Automatic Recovery

Failure handling is automatic:

1. Retry the current route within its remaining budget when the failure is transient or correctable.
2. Reroute to another allowed agentic execution profile.
3. Downgrade to guided execution while preserving validated work and state.
4. Use advisory output to help Orchestrator complete a step-driven fallback.
5. Pause only when all safe routes are exhausted, required authority is missing, or the user goal must change.

Each attempt records requested and actual mode, selected route, timestamps, tool usage, budget consumption, result, downgrade reason, changed files, tests, and handoff.

## Relationship to Role-Based Teams

Role-based teams answer **which engineering perspectives are needed**. Capability-aware execution answers **how each bounded assignment should run**. They are complementary and independently optional:

- Standard workflow may still use an agentic builder when useful.
- Role-based team mode may mix agentic, guided, and advisory workers.
- One agent may cover multiple compatible roles, but each dispatched package has one explicit role and objective.

## Compatibility and Failure Behavior

- Existing model metadata without execution fields defaults conservatively to guided or advisory behavior.
- Existing jobs without work-package state keep the current builder workflow.
- Unknown capability data never grants additional tools or permissions.
- Capability probes must not mutate project files.
- Invalid or incomplete handoffs are rejected and retried or rerouted.
- Safe validated edits may be retained across fallback; ambiguous partial work is isolated for review.

## Verification

Tests cover execution-profile resolution, conservative defaults, role-aware routing, allowed-model constraints, agentic preference for suitable builder/debugger work, automatic fallback, attempt records, dependency scheduling, file-overlap serialization, handoff validation, legacy-job compatibility, and user-facing documentation.
