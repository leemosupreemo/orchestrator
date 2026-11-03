#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from common import (
    ROOT,
    JOBS_DIR,
    gh_text,
    make_job_paths,
    now_iso,
    read_json,
    write_json,
    write_text,
    timestamp,
)
from orchestrator.project_config import PROJECT_CONFIG

def create_subtask_issue(title: str, body: str, parent_issue: int) -> int:
    labels = ["job:bug", "source:splinter", "status:planned"]
    label_flags = []
    for label in labels:
        label_flags.extend(["--label", label])
    
    full_body = f"{body}\n\n---\n**Parent Issue**: #{parent_issue}"
    
    out = gh_text(
        "issue",
        "create",
        "--title",
        title,
        "--body",
        full_body,
        *label_flags,
    )
    issue_number = int(out.rstrip("/").split("/")[-1])
    return issue_number

def splinter_job(job_path: Path) -> list[str]:
    job = read_json(job_path)
    
    if job.get("type") != "feature-plan" or "plan" not in job or "tasks" not in job["plan"]:
        print(f"!!! Job {job.get('job_id')} is not a feature-plan with tasks. Skipping.")
        return []

    parent_id = job["job_id"]
    parent_issue = job.get("issue_number")
    github = PROJECT_CONFIG.uses_github and bool(parent_issue)
    tasks = job["plan"]["tasks"]
    
    print(f"\n--- Splintering Parent Job: {parent_id} ({len(tasks)} tasks) ---")
    
    subtask_ids = []
    
    for i, task in enumerate(tasks, start=1):
        sub_title = f"[Sub-task {i}] {job['title']}: {task['title']}"
        print(f"      - Creating sub-task: {task['title']}...")
        
        # Build a "bug-fix" style plan for the sub-task so builder can implement it
        sub_plan = {
            "title": sub_title,
            "summary": task["description"],
            "repro_steps": [],
            "expected_behavior": "Implement as described in the task breakdown.",
            "acceptance_criteria": task["acceptance_criteria"],
            "constraints": job["plan"].get("constraints", []),
            "complexity": task["complexity"],
            "recommended_job_type": "bug-fix",
            "likely_files": task["likely_files"],
            "test_recommendations": task["tests"]
        }
        
        # Create GitHub Issue
        sub_body = f"## Description\n{task['description']}\n\n## Acceptance Criteria\n"
        sub_body += "\n".join([f"- {ac}" for x in task["acceptance_criteria"] for ac in (x if isinstance(x, list) else [x])])
        
        if github:
            sub_issue_number = create_subtask_issue(sub_title, sub_body, parent_issue)
            sub_ref = str(sub_issue_number)
        else:  # plain git: no sub-issues, a local reference names each sub-job
            sub_issue_number, sub_ref = None, secrets.token_hex(3)

        sub_job_id = f"{timestamp()}-task-{sub_ref}"
        paths = make_job_paths(sub_job_id)
        
        sub_job = {
            "job_id": sub_job_id,
            "parent_job_id": parent_id,
            "type": "bug-fix", # Treat sub-tasks as bug-fixes for implementation consistency
            "source": "splinter",
            "issue_number": sub_issue_number,
            "job_ref": sub_ref,
            "code_host": "github" if sub_issue_number else "git",
            "title": sub_title,
            "status": "planned",
            "planner": job["planner"],
            "builder": job["builder"],
            "reviewer": job["reviewer"],
            "branch": None,
            "pr_number": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "raw_input": task["description"],
            "plan": sub_plan,
        }
        
        write_json(paths.job_file, sub_job)
        # Create a brief.md for the sub-task
        from new_job import build_bug_issue_body
        write_text(paths.brief_file, build_bug_issue_body(sub_plan))
        
        subtask_ids.append(sub_job_id)
        print(f"        ✅ Created job: {sub_job_id}" + (f" (#{sub_issue_number})" if sub_issue_number else ""))

    # Mark parent as decomposed
    job["status"] = "decomposed"
    job["subtask_job_ids"] = subtask_ids
    job["updated_at"] = now_iso()
    write_json(job_path, job)
    
    print(f"\nDone! Parent job marked as 'decomposed'. {len(subtask_ids)} sub-tasks ready.")
    return subtask_ids

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file", help="Path to the parent job JSON file")
    args = parser.parse_args()

    splinter_job(Path(args.job_file))

if __name__ == "__main__":
    main()
