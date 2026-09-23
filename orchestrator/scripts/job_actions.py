#!/usr/bin/env python3
"""Non-menu entry points for job actions that otherwise live only in the
console, so the web UI (and scripts) can run them. Each one reuses the
console's own implementation rather than duplicating it.

    job_actions.py merge  <job.json>                  # Merge & Mark Completed
    job_actions.py answer <job.json> --answer TEXT    # answer the planner's question
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from common import read_json, record_clarification, write_json

SCRIPTS_DIR = Path(__file__).resolve().parent

# Same mapping the console uses when re-planning after a clarification.
REPLAN_TYPES = {
    "bug-fix": "bug",
    "bug-investigate": "bug",
    "feature-plan": "feature",
    "test-audit": "coverage",
    "feature-design": "design",
}


def load_job(path: Path) -> dict:
    job = read_json(path)
    job["_path"] = path
    return job


def merge(path: Path) -> int:
    from dev_console import handle_merge_cleanup

    handle_merge_cleanup(load_job(path))
    return 0


def answer(path: Path, text: str) -> int:
    job = read_json(path)
    question = job.get("human_clarification_question")
    if job.get("status") != "human-needed" or not question:
        print("This job isn't waiting on a question.")
        return 1
    record_clarification(job, question, text)
    write_json(path, job)
    print(f"Answer recorded. Re-planning with it...\n")
    replan_type = REPLAN_TYPES.get(job.get("type"), "feature")
    feedback = f"### USER CLARIFICATION ###\n{text}"
    return subprocess.call([sys.executable, str(SCRIPTS_DIR / "new_job.py"), replan_type, "--no-dispatch",
                            "--update", str(path), "--feedback", feedback])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    merge_p = sub.add_parser("merge", help="Merge the job's PR, clean up its branch and archive it")
    merge_p.add_argument("job_file")
    answer_p = sub.add_parser("answer", help="Answer the planner's clarification question and re-plan")
    answer_p.add_argument("job_file")
    answer_p.add_argument("--answer", required=True)
    args = parser.parse_args(argv)

    path = Path(args.job_file).resolve()
    if not path.is_file():
        print(f"Job not found: {path}")
        return 1
    if args.action == "merge":
        return merge(path)
    return answer(path, args.answer.strip())


if __name__ == "__main__":
    raise SystemExit(main())
