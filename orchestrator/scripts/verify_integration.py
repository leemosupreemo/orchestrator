#!/usr/bin/env python3
"""Merge a feature's job branches in a scratch worktree, build and test the result, and store the outcome."""
from __future__ import annotations

import argparse
import sys

from common import ROOT, JOBS_DIR, ORCHESTRATOR_RUNTIME_DIR, read_json
from orchestrator import features as feature_store
from orchestrator import integration_check
from orchestrator.project_config import PROJECT_CONFIG

DONE = {"discarded", "archived"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("feature")
    args = ap.parse_args()
    runtime = ORCHESTRATOR_RUNTIME_DIR
    features = feature_store.load(runtime)
    feature = feature_store.get(features, args.feature)
    branches = []
    for path in sorted(JOBS_DIR.glob("*.json")):
        if "_task_" in path.stem:
            continue
        job = read_json(path)
        if job.get("feature") == feature["id"] and job.get("branch") and job.get("status") not in DONE and job["branch"] not in branches:
            branches.append(job["branch"])
    base = PROJECT_CONFIG.base_branch or "main"
    print(f"Feature {feature['name']}: combining {len(branches)} branch(es) onto {base} in a scratch checkout (yours is not touched).")
    result = integration_check.verify(ROOT, base, branches, PROJECT_CONFIG.build_command, PROJECT_CONFIG.test_command, log=print)
    integration_check.save_result(runtime, feature["id"], result)
    print(f"\nResult: {result['status']}")
    for c in result["conflicts"]:
        print(f"  conflict: {c['branch']} ({', '.join(c['files']) or 'files unknown'})")
    for key in ("build", "test"):
        if result[key].get("ran") and not result[key]["ok"]:
            print(f"\n{key} output:\n{result[key]['tail']}")
    return 0 if result["status"] in {"pass", "nothing"} else 1


if __name__ == "__main__":
    sys.exit(main())
