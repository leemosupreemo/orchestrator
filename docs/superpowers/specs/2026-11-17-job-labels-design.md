# Concise Web UI job labels

The user approved consolidating job labels with “continue”. Use twelve labels:
Needs plan, Ready, Needs approval, Queued, Building, Needs input, Needs review,
Paused, Blocked, Needs a fix, Done, Moved.

Keep the existing reasons, recovery steps, action buttons, internal statuses,
grouping, and automation behavior. Put specific blocker titles, task numbers,
and review findings in the reason instead of the badge. Preserve distinctions
between questions, approvals, reviews, recoverable pauses, and problems.

## Approaches

Shortening every existing label would preserve too many similar badges. The
approved approach consolidates badges and retains specifics in descriptions.
Workflow redesign and changes to activity/run labels are outside this pass.

Implementation choice: expose an `in_progress` boolean in job state so Home and
bulk YOLO actions keep their existing membership after multiple states become
Paused. Keep epic plan readiness based on the underlying job and action.
