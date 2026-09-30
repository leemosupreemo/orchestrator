# Role-Based Team Development Approach

## Summary

Orchestrator will offer **Role-based team** as a development approach alongside the existing **Standard workflow**. The console recommends and preselects Role-based team for new jobs, while keeping Standard available as the lower-cost, lower-coordination option.

Role-based team is not a collection of personalities. It is a small, automatically assembled set of engineering responsibilities operating from one shared understanding of the user's goal. The approach must remain useful for simple jobs by allowing one agent to cover several compatible roles.

## User Experience

Job creation presents a concise development-approach choice:

- **Role-based team — Recommended:** Select the smallest set of engineering roles suited to the job. Roles coordinate through structured handoffs. This can take longer and use more AI capacity.
- **Standard workflow:** Use the existing planning, implementation, and review sequence.

Role-based team is preselected. Advanced options allow users to pin a custom set of roles. When no custom roster is supplied, Orchestrator analyzes the request and repository-facing plan signals to assemble the roster automatically.

The job screen displays the approach, active roles, and the reason each situational role was selected. A team-management action remains available while the job is in progress. User changes apply to subsequent handoffs and are recorded in job state.

## Team Model

The foundation consists of:

- **Technical Lead:** Owns architectural coherence, resolves conflicting recommendations, and keeps work aligned to the shared goal.
- **Implementation Engineer:** Owns code and test implementation.
- **QA Engineer:** Independently validates the result against the original user goal and acceptance criteria.

Situational roles are added only when request or plan evidence justifies a distinct contribution:

- UX Designer
- Frontend Engineer
- Backend Engineer
- Database Specialist
- Security Engineer
- Accessibility Specialist
- Performance Engineer
- DevOps Engineer
- Platform Specialist

Every role has a stable identifier, label, responsibility, activation signals, and selection reason. Automatic assembly is deterministic and capped to prevent team inflation. User-pinned roles are never removed automatically.

## Shared Understanding

Every role receives an Intent Brief containing:

- The user's original request
- The intended user outcome
- Acceptance criteria
- Constraints and non-goals
- Important assumptions and risks

The Intent Brief is generated from the original request and grounded plan. It is persisted in the job file so resumed work uses the same anchor.

Role communication uses structured handoffs:

- Finding or decision
- Evidence
- Impact on the user goal
- Action required from the next role
- Remaining risk or disagreement

The initial implementation carries this contract in the planning, building, and review prompts. Existing planner, builder, and reviewer agents perform the foundation roles; situational roles provide explicit perspectives and review obligations without requiring one model invocation per role.

## Data Model

Each job may contain:

```json
{
  "development_approach": "role-based",
  "team": {
    "mode": "auto",
    "auto_adjust": true,
    "selection_text": "...",
    "roles": [
      {
        "id": "technical_lead",
        "label": "Technical Lead",
        "required": true,
        "pinned": false,
        "reason": "Foundation role for architectural coherence."
      }
    ],
    "intent_brief": {
      "user_request": "...",
      "current_request": "...",
      "user_outcome": "...",
      "acceptance_criteria": [],
      "constraints": [],
      "assumptions": [],
      "risks": []
    },
    "handoffs": [
      {
        "finding": "...",
        "evidence": "...",
        "impact": "...",
        "action_required": "...",
        "remaining_risk": "...",
        "created_at": "..."
      }
    ],
    "updated_at": "..."
  }
}
```

Legacy jobs without these fields remain Standard workflow jobs.

## Selection Rules

Selection considers the request, job type, likely files, risks, task text, task acceptance criteria, and tests. Foundation roles are always present in Role-based team jobs. Situational roles use weighted explicit signals such as UI terminology, persistence or migration language, API/server changes, authentication or sensitive-data work, accessibility requirements, performance concerns, CI/deployment changes, and platform-specific code. One generic term is insufficient; either a strong domain signal or multiple supporting weak signals are required.

Automatic roles are deduplicated and capped. After planning, the entire automatic roster is re-ranked from the combined request and plan evidence so a newly justified specialist can replace a weaker early inference. User-pinned roles count toward the compact roster limit, are never removed automatically, and retain the foundation.

## Failure Behavior

- Unknown role identifiers are ignored instead of corrupting job state.
- Empty custom rosters retain the foundation roles.
- Missing team data falls back to Standard behavior.
- Role selection never blocks job creation.
- A failed or uncertain inference retains the foundation team and exposes the limitation through selection reasons.

## Verification

Automated tests must cover deterministic role selection, team-size limits, custom role pinning, Intent Brief generation, prompt propagation, CLI persistence, console approach selection, live roster editing, and legacy-job compatibility. Canonical repository validation remains `python3 -m compileall -q orchestrator tests` followed by `python3 -m unittest discover -s tests`.
