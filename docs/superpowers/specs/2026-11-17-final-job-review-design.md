# Final job review and delivery

Approved by the user's “yes continue”. Keep the existing five stages and make
the final stage Review & approve. Verified jobs stay open until a person approves
completion or merge. YOLO never supplies final approval.

When delivery is configured for an app-producing job, require a successful
receipt for this job's current branch and commit. Pending, failed, and stale
deliveries offer Deliver/Retry and block final approval. Without delivery (or for
tests/design-only work), explicitly say it is unavailable and allow approval.
Show the matching delivery's safe preview/Firebase link and build details.

Require an explicit checked acknowledgment before completing or merging from
any Web UI entry point. Enforce the same requirement at the API/script boundary,
reject stale review revisions and unfinished task lists, and save reviewer,
timestamp, revision, and delivery information in the completed job record.
Existing CLI callers attest using --reviewed; completion is never an AI step.

Approach: reuse the existing merge/complete actions and delivery mechanism with
a shared review-state helper. A separate sixth stage would duplicate the final
review step. Do not upload builds, merge branches, or complete real user jobs as
part of implementing/testing this change. Existing unrelated edits stay intact.
