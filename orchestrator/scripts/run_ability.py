#!/usr/bin/env python3
"""Run one of an AI tool's highlighted abilities (tool_abilities.HIGHLIGHTS) on a job: a review of its branch by Claude Code
or Codex. It runs where the branch is checked out (the job's own worktree, or the project folder when it's on that branch),
otherwise in a scratch checkout on a short-lived branch at the job's commit, so the project folder is never touched. A
headless review's text is saved with the job's documents."""
from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
from pathlib import Path

from common import OUTPUT_DIR, ROOT, read_json
from orchestrator import tool_abilities, worktrees
from orchestrator.project_config import PROJECT_CONFIG


@contextlib.contextmanager
def branch_checkout(job_id: str, branch: str):
    """A folder with the job's branch checked out, as a real branch (reviews of "the current branch" need one)."""
    folder = worktrees.checked_out_in(ROOT, branch)  # the job's own worktree, or the project folder
    if folder is not None:
        yield folder
        return
    review = f"orchestrator-review/{job_id}"
    with worktrees.scratch(ROOT, branch, prefix="orchestrator-review-") as work:
        subprocess.run(["git", "checkout", "-q", "-B", review], cwd=work, check=True)
        try:
            yield work
        finally:
            subprocess.run(["git", "checkout", "-q", "--detach"], cwd=work, capture_output=True)
            subprocess.run(["git", "branch", "-D", review], cwd=ROOT, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_file")
    ap.add_argument("ability")
    args = ap.parse_args()
    job_path = Path(args.job_file)
    job = read_json(job_path)
    try:
        ability = tool_abilities.find(args.ability)
    except KeyError:
        print(f"Unknown ability: {args.ability}")
        return 2
    branch = str(job.get("branch") or "").strip()
    if not branch:
        print("This job has no branch yet, so there are no changes to review.")
        return 1
    base = job.get("base_branch") or PROJECT_CONFIG.base_branch or "main"
    argv = tool_abilities.command(ability, branch, base, job.get("pr_number"))
    print(f"{ability['label']}: {branch} against {base}\n$ {' '.join(argv)}\n", flush=True)
    with branch_checkout(job_path.stem, branch) as folder:
        if ability.get("mode") == "terminal":  # the tool asks before it bills: it runs in this run's terminal
            return subprocess.call(argv, cwd=folder)
        result = subprocess.run(argv, cwd=folder, capture_output=True, text=True)
    text = (result.stdout or "").strip()
    print(text or result.stderr.strip(), flush=True)
    if result.returncode == 0 and text:
        out = OUTPUT_DIR / str(job.get("job_id") or job_path.stem)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{ability['id']}.md").write_text(f"# {ability['label']}\n\nBranch `{branch}` against `{base}`.\n\n{text}\n", encoding="utf-8")
        print(f"\nSaved with the job's documents ({ability['id']}.md).")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
