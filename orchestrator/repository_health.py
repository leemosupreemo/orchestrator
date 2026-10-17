"""Read-only repository diagnostics and narrowly scoped GitHub label repairs.

The page renders saved status locally and refreshes it in a background task. Checks
never push commits or create probe issues/PRs; GitHub remains authoritative for merge policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from orchestrator.code_host import parse_remote

LABELS = {
    "job:bug": "Bug fix", "job:coverage": "Test coverage work", "job:design": "Design work",
    "job:feature": "Feature work", "job:quick": "Quick change",
    "source:manual": "Created directly", "source:splinter": "Split from another job",
    "status:planned": "Plan ready", "status:designing": "Design in progress",
    "status:executing": "Work in progress", "status:debugging": "Debugging in progress",
    "status:fix-requested": "Revision requested", "status:human-needed": "Needs your attention",
    "status:review-needed": "Ready for review",
}


class RepositoryError(Exception):
    pass


def run(root: Path, argv: list[str], *, payload: dict | None = None, timeout: int = 15) -> str:
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, errors="replace",
                                input=json.dumps(payload) if payload is not None else None,
                                timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"})
    except subprocess.TimeoutExpired:
        raise RepositoryError("The connection timed out. Check network access and try again.")
    except OSError:
        raise RepositoryError(f"{argv[0]} is not installed on this computer.")
    if result.returncode:
        detail = re.sub(r"(https?://)[^/@\s]+@", r"\1", (result.stderr or result.stdout).strip())
        raise RepositoryError(detail[-500:] or "The repository request failed.")
    return result.stdout


def gh_api(root: Path, endpoint: str, *, method: str = "GET", values: dict | None = None, paginate: bool = False):
    argv = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    if paginate:
        argv += ["--paginate", "--slurp"]
    if values is not None:
        argv += ["--input", "-"]
    try:
        data = json.loads(run(root, argv, payload=values))
        return [item for page in data for item in page] if paginate else data
    except (ValueError, TypeError):
        raise RepositoryError("GitHub returned an unreadable response. Try checking again.")


def github_repo(origin: str, mode: str) -> str:
    parsed = parse_remote(origin)
    if mode != "github" or not parsed or parsed[0] != "github.com" or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parsed[1]):
        raise RepositoryError("GitHub integration requires a github.com repository and the GitHub job workflow.")
    return parsed[1]


def identity(origin: str, mode: str) -> str:
    return hashlib.sha256(f"{mode}\0{origin}".encode()).hexdigest()


def read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each writer has its own temporary file; readers never see a partial result.
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        json.dump(data, file)
        temporary = Path(file.name)
    temporary.replace(path)


def saved_check(runtime: Path, origin: str, mode: str) -> dict | None:
    result = read(runtime / "state" / "repository-health.json")
    return result if result.get("identity") == identity(origin, mode) else None


def label_status(existing: list[dict]) -> list[dict]:
    names = {str(label.get("name", "")).casefold() for label in existing}
    return [{"name": name, "description": meaning, "present": name.casefold() in names} for name, meaning in LABELS.items()]


def check_connection(root: Path, runtime: Path, origin: str, mode: str) -> dict:
    checks = []
    if origin:
        try:
            run(root, ["git", "ls-remote", "--heads", "origin"])
            checks.append({"id": "read", "label": "Repository read access", "status": "ok", "detail": "Remote branches can be read."})
        except RepositoryError as error:
            checks.append({"id": "read", "label": "Repository read access", "status": "blocked", "detail": str(error)})
    else:
        checks.append({"id": "read", "label": "Repository read access", "status": "local", "detail": "No origin remote. Work stays on this computer."})
    labels = []
    if mode == "github":
        try:
            repo = github_repo(origin, mode)
            metadata = gh_api(root, f"repos/{repo}")
            writable = metadata.get("permissions", {}).get("push")
            archived = bool(metadata.get("archived"))
            checks.append({"id": "publish", "label": "Code publishing", "status": "blocked" if archived or writable is False else "unknown",
                           "detail": "This repository is archived." if archived else "This account has read-only repository access." if writable is False else
                           "GitHub reports write permission; Git credentials and branch rules are checked when publishing." if writable else "Write access has not been verified.",
                           "remedy": "Unarchive the repository in GitHub settings, or choose another repository." if archived else "Ask a repository owner to grant this account write access." if writable is False else "Write credentials and branch rules are verified when a job publishes."})
            checks.append({"id": "issues", "label": "Issues and pull requests", "status": "blocked" if archived or not metadata.get("has_issues") or writable is False else "unknown",
                           "detail": "Enable Issues in GitHub repository settings." if not metadata.get("has_issues") else "A writable repository is required for this workflow." if archived or writable is False else
                           "Issues are enabled. Token permissions are checked when a job creates an issue or PR; no test items were created.",
                           "remedy": "Enable Issues in repository settings." if not metadata.get("has_issues") else "Restore repository write access to use the GitHub job workflow." if archived or writable is False else "Issue and pull request token access is checked when a job needs it."})
            labels = label_status(gh_api(root, f"repos/{repo}/labels", paginate=True))
        except RepositoryError as error:
            checks.append({"id": "github", "label": "GitHub API access", "status": "blocked", "detail": str(error),
                           "remedy": "Sign in with GitHub CLI (`gh auth login`) and grant this account access to the repository."})
            if not any(c["id"] == "issues" for c in checks):
                checks.append({"id": "issues", "label": "Issues and pull requests", "status": "blocked", "detail": "Restore GitHub API access before running GitHub jobs."})
        checks.append({"id": "merge", "label": "Merge eligibility", "status": "per_job", "detail": "Checked on each pull request. GitHub reviews, required checks and branch rules apply."})
    else:
        checks.extend([
            {"id": "publish", "label": "Code publishing", "status": "unknown" if origin else "local", "detail": "Write access is checked when a job pushes its branch." if origin else "Jobs keep their branches on this computer."},
            {"id": "merge", "label": "Merge eligibility", "status": "local", "detail": "Git mode merges locally. Review and merge on your host to use its approval rules."},
        ])
    result = {"identity": identity(origin, mode), "checked_at": time.time(), "checks": checks, "labels": labels}
    save(runtime / "state" / "repository-health.json", result)
    return result


def repair_labels(root: Path, runtime: Path, origin: str, mode: str) -> dict:
    repo = github_repo(origin, mode)
    labels = label_status(gh_api(root, f"repos/{repo}/labels", paginate=True))
    created = []
    for label in labels:
        if label["present"]:
            continue
        color = "0052CC" if label["name"].startswith("job:") else "6B7280" if label["name"].startswith("source:") else "0E8A16"
        try:
            gh_api(root, f"repos/{repo}/labels", method="POST", values={"name": label["name"], "description": label["description"], "color": color})
        except RepositoryError:
            # Another request may have created it after the initial read. Never overwrite it.
            current = label_status(gh_api(root, f"repos/{repo}/labels", paginate=True))
            if not next(l["present"] for l in current if l["name"] == label["name"]):
                raise
        else:
            created.append(label["name"])
    labels = label_status(gh_api(root, f"repos/{repo}/labels", paginate=True))
    prior = saved_check(runtime, origin, mode)
    if prior:
        prior.update(labels=labels, labels_checked_at=time.time())
        save(runtime / "state" / "repository-health.json", prior)
    return {"created": created, "labels": labels, "detail": f"Created {len(created)} missing labels. Existing labels were preserved."}


def configuration_status(root: Path, runtime: Path, base: str) -> dict:
    try:
        relative = runtime.relative_to(root).as_posix() + "/project.json"
    except ValueError:
        return {"status": "unknown", "detail": "Project configuration is stored outside this repository."}
    reference = f"refs/remotes/origin/{base}"
    try:
        run(root, ["git", "rev-parse", "--verify", reference])
    except RepositoryError:
        return {"status": "unknown", "detail": "Fetch remote branches to compare project configuration."}
    try:
        committed = run(root, ["git", "show", f"{reference}:{relative}"])
    except RepositoryError:
        return {"status": "untracked", "detail": "Project configuration is not committed on the last fetched base branch."}
    local = (runtime / "project.json").read_text(errors="replace") if (runtime / "project.json").is_file() else ""
    matches = committed == local
    return {"status": "matches" if matches else "different", "path": relative,
            "detail": "Matches the last fetched base branch." if matches else "Local configuration differs from the last fetched base branch."}


def publishing_jobs(root: Path, runtime: Path, origin: str, mode: str, base: str = "main") -> dict:
    parsed = parse_remote(origin)
    web = f"https://{parsed[0]}/{parsed[1]}" if parsed else ""
    problems, recent = [], []
    for file in sorted((runtime / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
        if re.search(r"_task_\d+$", file.stem):
            continue
        job = read(file)
        if not job or job.get("status") in {"completed", "archived", "discarded"}:
            continue
        branch = job.get("branch")
        if not branch:
            continue
        job_mode = job.get("code_host") or mode
        missing_pr = job_mode == "github" and job.get("status") == "review-needed" and branch.lower() not in {"main", "master", base.lower(), str(job.get("base_branch") or base).lower()} and not job.get("pr_number")
        problem = job.get("publish_error") or ("Work is ready for review, but no pull request is recorded." if missing_pr else "")
        issue = str(job.get("issue_number") or "")
        pr = str(job.get("pr_number") or "")
        detail = problem or ("Pull request recorded" if pr else "Branch published" if job.get("last_published_at") else "Publishing is recorded when a job finishes.")
        item = {"id": file.stem, "title": job.get("title") or file.stem, "branch": branch,
                "detail": detail,
                "problem": bool(problem), "last_published_at": job.get("last_published_at"),
                "issue_url": f"{web}/issues/{issue}" if web and parsed[0] == "github.com" and issue.isdigit() else "",
                "pr_url": f"{web}/pull/{pr}" if web and parsed[0] == "github.com" and pr.isdigit() else ""}
        (problems if problem else recent).append(item)
    return {"problems": problems[:10], "recent": recent[:5]}


def republish(root: Path, runtime: Path, origin: str, mode: str, job_file: Path, base: str) -> dict:
    if job_file.is_symlink() or not job_file.resolve().is_relative_to((runtime / "jobs").resolve()):
        raise RepositoryError("The job must belong to this project.")
    job = read(job_file)
    updates = {}
    branch = str(job.get("branch") or "")
    project_base = base
    base = str(job.get("base_branch") or base)
    if job.get("status") != "review-needed" and not (job.get("status") == "human-needed" and job.get("publish_error")):
        raise RepositoryError("Only finished work awaiting review can be republished. Open the job to finish its work first.")
    if not origin or not branch or branch.lower() in {"main", "master", base.lower(), project_base.lower()}:
        raise RepositoryError("Publishing requires a remote and a dedicated job branch.")
    run(root, ["git", "check-ref-format", "--branch", branch])
    run(root, ["git", "show-ref", "--verify", f"refs/heads/{branch}"])
    github = (job.get("code_host") or mode) == "github"
    repo = github_repo(origin, "github") if github else ""
    def update_job(values):
        latest = read(job_file)
        if not latest or latest.get("branch") != branch or job_file.is_symlink():
            raise RepositoryError("The job changed while publishing. Refresh it before retrying.")
        latest.update(values)
        save(job_file, latest)
    try:
        run(root, ["git", "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], timeout=30)
        if github:
            output = run(root, ["gh", "pr", "list", "--repo", repo, "--head", branch, "--base", base,
                                "--state", "open", "--json", "number,url", "--limit", "1"])
            try:
                existing = json.loads(output)
            except ValueError:
                raise RepositoryError("GitHub returned an unreadable PR list. Try again.")
            if existing:
                url = existing[0].get("url", "")
            else:
                issue = str(job.get("issue_number") or "")
                body = f"Orchestrator job: {job_file.stem}\n\n" + (f"Refs #{issue}\n" if issue.isdigit() else "")
                url = run(root, ["gh", "pr", "create", "--repo", repo, "--draft", "--base", base, "--head", branch,
                                 "--title", f"AI draft: {job.get('title') or job_file.stem}", "--body", body]).strip()
            match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/(\d+)", url)
            if not match:
                raise RepositoryError("GitHub did not return a pull request for this repository.")
            updates.update(pr_number=int(match.group(1)), pr_url=url)
        else:
            from orchestrator.code_host import review_request_url
            updates["merge_request_url"] = review_request_url(origin, branch, base)
        updates.update(publish_error=None, last_published_at=time.time(), published_commit=run(root, ["git", "rev-parse", f"refs/heads/{branch}"]).strip())
        update_job(updates)
    except RepositoryError as error:
        update_job({"publish_error": str(error)})
        raise
    return {"detail": "Job branch published. Open the job to review the work."}
