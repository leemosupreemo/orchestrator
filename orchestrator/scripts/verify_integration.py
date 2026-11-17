#!/usr/bin/env python3
"""Merge a feature's job branches (or, with --plan, every branch of the plan being built) in a scratch worktree, build and
test the result, and store the outcome. A plan whose branches work together is kept as the branch integration/plan-<id>, so
that one build can be sent to testers."""
from __future__ import annotations

import argparse
import sys

from common import ROOT, JOBS_DIR, ORCHESTRATOR_RUNTIME_DIR, read_json
from orchestrator import features as feature_store
from orchestrator import integration_check, plan_run
from orchestrator.project_config import PROJECT_CONFIG

DONE = {"discarded", "archived"}


def branches_of(feature_ids: set[str]) -> list[str]:
    """The open job branches of these features, once each."""
    branches = []
    for path in sorted(JOBS_DIR.glob("*.json")):
        if "_task_" in path.stem:
            continue
        job = read_json(path)
        if job.get("feature") in feature_ids and job.get("branch") and job.get("status") not in DONE and job["branch"] not in branches:
            branches.append(job["branch"])
    return branches


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("feature", nargs="?")
    ap.add_argument("--plan", action="store_true", help="every branch of the plan being built (Build the plan)")
    args = ap.parse_args()
    runtime = ORCHESTRATOR_RUNTIME_DIR
    base = PROJECT_CONFIG.base_branch or "main"
    if args.plan:
        run = plan_run.load(runtime)
        if not run:
            print("No plan is being built.")
            return 1
        key, keep_as, what = integration_check.PLAN_KEY, f"integration/plan-{run['id']}", "The plan"
        branches = branches_of(set(run.get("features") or []))
    else:
        if not args.feature:
            ap.error("name a feature, or use --plan")
        feature = feature_store.get(feature_store.load(runtime), args.feature)
        key, keep_as, what = feature["id"], None, f"Feature {feature['name']}"
        branches = branches_of({feature["id"]})
    print(f"{what}: combining {len(branches)} branch(es) onto {base} in a scratch checkout (yours is not touched).")
    result = integration_check.verify(ROOT, base, branches, PROJECT_CONFIG.build_command, PROJECT_CONFIG.test_command, log=print, keep_as=keep_as)
    integration_check.save_result(runtime, key, result)
    print(f"\nResult: {result['status']}")
    for c in result["conflicts"]:
        print(f"  conflict: {c['branch']} ({', '.join(c['files']) or 'files unknown'})")
    for key in ("build", "test"):
        if result[key].get("ran") and not result[key]["ok"]:
            print(f"\n{key} output:\n{result[key]['tail']}")
    return 0 if result["status"] in {"pass", "nothing"} else 1


if __name__ == "__main__":
    sys.exit(main())
