#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from common import OUTPUT_DIR, ROOT, gh_json, gh_text, read_json, run
from orchestrator.project_config import PROJECT_CONFIG


def open_or_update_pr(job_path: Path) -> tuple[int | None, str | None]:
    job = read_json(job_path)
    issue_number = job.get("issue_number")
    branch = job.get("branch")
    if not branch:
        return None, None

    brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"

    try:
        run(["git", "push", "-u", "origin", branch], cwd=ROOT)
    except Exception as e:
        print(f"Warning: git push failed: {e}")

    existing = []
    try:
        existing = gh_json("pr", "list", "--head", branch, "--json", "number,title,body")
    except Exception:
        existing = []
    
    debug_summary = ""
    if job.get("debug_history"):
        debug_summary = "\n## Debug History\n\n"
        for entry in job["debug_history"]:
            debug_summary += f"### Iteration {entry['iteration']}\n"
            debug_summary += f"- **Hypothesis**: {entry.get('hypothesis', 'N/A')}\n"
            debug_summary += f"- **Action**: `{entry.get('action', 'N/A')}`\n"
            result = entry.get("result", {})
            if isinstance(result, dict):
                debug_summary += f"- **Result**: Build {'✅' if result.get('build_ok') else '❌'}, Tests {'✅' if result.get('tests_ok') else '❌'}\n"
                if result.get("notes"):
                    debug_summary += f"  - *Notes*: {result['notes']}\n"
            else:
                debug_summary += f"- **Result**: {result}\n"
            debug_summary += "\n"

    issue_str = f"issue #{issue_number}" if issue_number else "task"
    body = f"""## What changed

See builder summary in repo artifacts.

## Why it changed

Fixes or advances {issue_str}.

{debug_summary}
## Files touched

See diff.

## Risks / edge cases

See builder summary.

## Testing performed

See orchestrator output for {job["job_id"]}/build.log and test.log if present.

AI_BRIEF_FILE: {brief_file}
"""

    matching_pr = None
    base_branch = (job.get("base_branch") or PROJECT_CONFIG.base_branch or "main").strip()
    is_base_branch = branch.lower() in ["main", "master", base_branch.lower()]

    if existing and isinstance(existing, list):
        for pr in existing:
            title_lower = pr.get("title", "").lower()
            body_lower = pr.get("body", "").lower()
            if issue_number and (
                f"issue #{issue_number}" in title_lower
                or f"issue #{issue_number}" in body_lower
                or f"fixes #{issue_number}" in body_lower
                or f"fixes or advances issue #{issue_number}" in body_lower
            ):
                matching_pr = pr
                break

        # If not on base branch and no explicit match, the dedicated feature branch's PR is ours
        if not matching_pr and not is_base_branch and len(existing) > 0:
            matching_pr = existing[0]

    if matching_pr:
        pr_number = matching_pr["number"]
        try:
            gh_text("pr", "edit", str(pr_number), "--body", body)
        except Exception as e:
            print(f"Warning: gh pr edit failed: {e}")
        pr_url = None
        try:
            pr_data = gh_json("pr", "view", str(pr_number), "--json", "url")
            pr_url = pr_data.get("url") if pr_data else None
        except Exception:
            pass
        return pr_number, pr_url

    # Avoid opening PR from base branch to base branch
    if is_base_branch:
        return None, None

    try:
        title_text = f'AI draft for issue #{issue_number}: {job.get("title", "")}' if issue_number else f'AI draft: {job.get("title", "")}'
        out = gh_text(
            "pr",
            "create",
            "--draft",
            "--title",
            title_text,
            "--body",
            body,
            "--base",
            base_branch,
            "--head",
            branch,
        )
        pr_url = out.strip()
        try:
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
        except Exception:
            pr_number = None
        return pr_number, pr_url
    except Exception as e:
        print(f"Warning: Failed to create PR: {e}")
        return None, None
