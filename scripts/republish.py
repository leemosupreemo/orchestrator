#!/usr/bin/env python3
"""Republish the hosted web app (firebase hosting) after checking that it is safe to.

    python3 scripts/republish.py                    # checks, then deploys
    python3 scripts/republish.py --dry-run          # the checks only
    python3 scripts/republish.py --verify isBackgroundRun   # then confirm the live app.js has this symbol
    python3 scripts/republish.py --only tests.test_web_ui   # a smaller suite (named modules) for a quick check
    python3 scripts/republish.py --allow-dirty      # deploy uncommitted edits under orchestrator/web/static on purpose
    python3 scripts/republish.py --skip-tests       # no test gate (says so); for a broken suite you are fixing

What it checks is described in orchestrator/republish_gate.py. Exit 0: deployed (or --dry-run passed). 1: blocked. 2: the deploy
itself failed or could not start.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator import republish_gate as gate  # noqa: E402


def main(argv: list[str] | None = None, *, root: Path = ROOT, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
         fetch: Callable[[str], str] | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="run the checks, don't deploy")
    parser.add_argument("--skip-tests", action="store_true", help="skip the test gate (the working-tree check still runs)")
    parser.add_argument("--allow-dirty", action="store_true", help=f"deploy uncommitted changes under {gate.STATIC_DIR}")
    parser.add_argument("--only", nargs="+", metavar="MODULE", help="run only these test modules, like tests.test_web_ui")
    parser.add_argument("--verify", metavar="SYMBOL", help="after deploying, check the live app.js contains this text")
    args = parser.parse_args(argv)

    dirty = gate.dirty_static(root, run)
    if dirty and not args.allow_dirty:
        out(f"Blocked: {len(dirty)} uncommitted change(s) under {gate.STATIC_DIR} would be uploaded, though no branch has them:")
        for path in dirty[:15]:
            out(f"  {path}")
        out("Commit them (or let the other session finish), or pass --allow-dirty to deploy them on purpose.")
        return 1
    if dirty:
        out(f"Note: deploying {len(dirty)} uncommitted change(s) under {gate.STATIC_DIR} (--allow-dirty).")

    if args.skip_tests:
        out("SKIPPING THE TEST GATE (--skip-tests): nothing checked that the web app works.")
    else:
        baseline = gate.read_baseline(root / gate.BASELINE_FILE)
        log_dir = root / ".orchestrator" / "output" / "republish"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.log"
        out(f"Running the {'selected' if args.only else 'whole'} test suite (the full suite takes several minutes)...")
        code, result = gate.run_suite(root, only=args.only, log=log, run=run)
        failing, ran = result["failing"], result["ran"]
        verdict = gate.judge(failing, baseline, returncode=code, ran=ran)
        if args.only:  # a subset can't say whether the baseline's other tests still fail
            verdict.fixed = []
        out(f"{ran} tests ran; {len(failing)} failed ({len(verdict.known)} already known, {len(verdict.new)} new). Full output: {log}")
        if verdict.fixed:
            out(f"{len(verdict.fixed)} known failure(s) pass now; delete their lines from {gate.BASELINE_FILE}:")
            for test in verdict.fixed:
                out(f"  {test}")
        if not verdict.ok:
            out("Blocked: the web app was not republished.")
            if verdict.problem:
                out(f"  {verdict.problem}")
            for test in verdict.new:
                out(f"  new failure: {test}")
            out(f"Fix them, or (if a failure is understood and acceptable) add it with a reason to {gate.BASELINE_FILE}.")
            return 1

    if args.dry_run:
        out("Dry run: the checks passed; nothing was deployed.")
        return 0
    if not shutil.which(gate.DEPLOY_COMMAND[0]):
        out(f"Can't deploy: `{gate.DEPLOY_COMMAND[0]}` isn't installed or isn't on PATH.")
        return 2
    out(f"Deploying: {' '.join(gate.DEPLOY_COMMAND)}")
    deployed = run(gate.DEPLOY_COMMAND, cwd=str(root), check=False)
    if deployed.returncode != 0:
        out(f"The deploy failed (exit {deployed.returncode}); see its output above.")
        return 2
    url = gate.hosting_url(root)
    out(f"Deployed{f' to {url}' if url else ''}.")
    if args.verify:
        if not url:
            out("Can't verify: no Firebase project found in .firebaserc.")
            return 2
        try:
            found = gate.served_count(url, args.verify, fetch)
        except OSError as exc:
            out(f"Couldn't fetch the live app to verify it: {exc}")
            return 2
        out(f"Live app.js contains '{args.verify}' {found} time(s).")
        if not found:
            out("It isn't there: the old version may still be cached, or the deploy didn't carry the change.")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
