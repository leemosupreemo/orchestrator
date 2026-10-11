#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import OUTPUT_DIR, PROMPTS_DIR, ROOT, gh_text, run, write_text
from llm import run_llm
from model_router import ModelRole

MAX_DIFF_CHARS = 100_000


def summarize_pr_files(pr_json: str) -> str:
    try:
        metadata = json.loads(pr_json)
    except json.JSONDecodeError:
        return "PR file metadata was not valid JSON."

    files = metadata.get("files") or []
    commits = metadata.get("commits") or []
    lines = [
        "GitHub did not provide a unified diff for this PR, so this review uses PR metadata.",
        f"Changed files: {len(files)}",
        f"Commits: {len(commits)}",
        "",
        "Files:",
    ]

    for file_info in files[:300]:
        path = file_info.get("path", "<unknown>")
        additions = file_info.get("additions", "?")
        deletions = file_info.get("deletions", "?")
        lines.append(f"- {path} (+{additions}/-{deletions})")

    if len(files) > 300:
        lines.append(f"- ... {len(files) - 300} additional file(s) omitted from metadata summary")

    return "\n".join(lines)


def load_pr_diff(pr_number: int, pr_json: str) -> str:
    try:
        diff_text = gh_text("pr", "diff", str(pr_number))
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        if "maximum number of files" not in output and "PullRequest.diff too_large" not in output:
            raise
        return summarize_pr_files(pr_json)

    if len(diff_text) > MAX_DIFF_CHARS:
        return (
            diff_text[:MAX_DIFF_CHARS]
            + f"\n\n...[review_ready truncated diff: {len(diff_text) - MAX_DIFF_CHARS} chars omitted]..."
        )
    return diff_text


def load_branch_diff(branch: str, base: str) -> tuple[str, str]:
    """For a job without a pull request (plain git): (metadata, diff) of the branch against its base, from this repo."""
    stat = run(["git", "diff", "--stat", f"{base}...{branch}"], cwd=ROOT).stdout
    commits = run(["git", "log", "--oneline", f"{base}..{branch}"], cwd=ROOT).stdout
    diff_text = run(["git", "diff", f"{base}...{branch}"], cwd=ROOT).stdout
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + f"\n\n...[review_ready truncated diff: {len(diff_text) - MAX_DIFF_CHARS} chars omitted]..."
    return f"Branch {branch} against {base}\n\nCommits:\n{commits}\nFiles:\n{stat}", diff_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pr_number", type=int, nargs="?", help="The pull request to review (GitHub)")
    parser.add_argument("--branch", help="Review this branch against --base instead of a pull request (plain git)")
    parser.add_argument("--base", default="main")
    parser.add_argument("--reviewer", default="gemini")
    parser.add_argument("--brief-file", required=True)
    parser.add_argument("--job-file", help="Path to job JSON file to update")
    args = parser.parse_args()

    if args.pr_number is None and not args.branch:
        parser.error("Give a pull request number, or --branch to review a branch.")
    brief = Path(args.brief_file).read_text(encoding="utf-8")
    if args.pr_number is not None:
        pr_json = gh_text("pr", "view", str(args.pr_number), "--json", "title,body,files,commits,url")
        diff_text = load_pr_diff(args.pr_number, pr_json)
    else:
        pr_json, diff_text = load_branch_diff(args.branch, args.base)
    reviewer_prompt = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")

    from orchestrator import prd
    prompt = f"""{reviewer_prompt}
{prd.context_block(ROOT, "reviewer")}
Brief:
{brief}

PR metadata:
{pr_json}

Diff:
{diff_text}
"""

    allowed_models = None
    job: dict = {}  # without --job-file there is no job to update, but the session bookkeeping below still runs
    if args.job_file:
        from common import now_iso, read_json, write_json
        job_path = Path(args.job_file)
        if job_path.exists():
            job = read_json(job_path)
            allowed_models = job.get("allowed_models")
            
    try:
        review, actual_reviewer, session_id = run_llm(args.reviewer, prompt, cwd=ROOT, timeout=300, role=ModelRole.REVIEWER, allowed_models=allowed_models)
        
        # Track session IDs in the job
        if "llm_sessions" not in job:
            job["llm_sessions"] = []
        job["llm_sessions"].append({"id": session_id, "model": actual_reviewer})
        print(f"Reviewer used: {actual_reviewer}")
    except Exception as e:
        print(f"❌ Review failed: {e}")
        sys.exit(1)

    if args.job_file:
        job = read_json(job_path) # Re-read in case it changed
        job["actual_reviewer_used"] = actual_reviewer
        job["updated_at"] = now_iso()
        write_json(job_path, job)

    out_dir = OUTPUT_DIR / (f"pr-{args.pr_number}" if args.pr_number is not None else f"branch-{args.branch.replace('/', '-')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "review.md"
    write_text(out_path, review)

    print(review)
    print(f"\nSaved review to {out_path}")

    if args.pr_number is not None:
        print(f"Posting review to PR #{args.pr_number}...")
        gh_text("pr", "comment", str(args.pr_number), "--body", review)
    print("Done.")


if __name__ == "__main__":
    main()
