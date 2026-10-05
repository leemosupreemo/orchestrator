"""The project's documentation, in one place, always generated from what is true now.

Three kinds of document:

  a job's page      what it set out to do, what was decided, what was built, how it was tested, what the review said
  a feature's page  what it is, who it serves, what it depends on, and every job that went into it
  the project's own files   README, AGENTS.md and the markdown under docs/, read as they are in the repository

Job and feature pages are composed on request from the job files and the feature store, never stored, so they cannot drift from
the work they describe and add no files to the repository. `export_all` joins everything into one markdown file.

Pure functions: callers gather the data, nothing here touches git or a model.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_FILE_CHARS = 200_000
MAX_PROJECT_FILES = 300
SKIP_DIRS = {".git", ".orchestrator", "node_modules", "build", "dist", "target", "venv", ".venv", "__pycache__", "Pods", "DerivedData", "designs"}
ROOT_NOTES = ("README.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "CONTRIBUTING.md", "CHANGELOG.md")


class DocsError(ValueError):
    pass


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "\n…(shortened)"


def _bullets(items: list[Any]) -> str:
    return "\n".join(f"- {str(i).strip()}" for i in items if str(i).strip())


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body.strip()}\n" if body and body.strip() else ""


STATUS_WORDS = {"planned": "Planned, waiting for approval", "scheduled": "Scheduled", "executing": "Running", "review-needed": "Built, ready for review",
                "completed": "Done", "human-needed": "Waiting on a person", "debugging": "Needs a fix", "failed": "Failed", "designing": "Being designed",
                "discarded": "Discarded"}


# --------------------------------------------------------------------------- one job


def job_doc(job: dict[str, Any], review: str = "", changed_files: list[str] | None = None, feature_name: str = "", links: list[dict[str, str]] | None = None) -> str:
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    tasks = [t for t in plan.get("tasks") or [] if isinstance(t, dict)]
    done = set(job.get("completed_task_indices") or [])
    status = STATUS_WORDS.get(str(job.get("status")), str(job.get("status") or "unknown"))
    head = [f"# {job.get('title') or job.get('job_id') or 'Job'}", ""]
    facts = [("Kind", str(job.get("type") or "").replace("-", " ")), ("Status", status), ("Feature", feature_name),
             ("Branch", f"`{job['branch']}`" if job.get("branch") else ""), ("Issue", f"#{job['issue_number']}" if job.get("issue_number") else ""),
             ("Pull request", f"#{job['pr_number']}" if job.get("pr_number") else ""), ("Started", str(job.get("created_at") or "")[:16].replace("T", " "))]
    head += [f"- **{k}:** {v}" for k, v in facts if v]
    out = ["\n".join(head) + "\n"]
    out.append(_section("What it is", plan.get("summary") or job.get("raw_input") or ""))
    out.append(_section("Assumptions the AI made", _bullets(plan.get("assumptions") or [])))
    out.append(_section("Constraints", _bullets(plan.get("constraints") or [])))
    out.append(_section("Risks", _bullets(plan.get("risks") or [])))

    decisions = []
    for h in job.get("clarification_history") or []:
        if isinstance(h, dict) and (h.get("question") or h.get("answer")):
            decisions.append(f"- **{str(h.get('question', '')).strip()}**\n  {str(h.get('answer', '')).strip()}")
    out.append(_section("Decisions you made", "\n".join(decisions)))

    ver = job.get("verification") if isinstance(job.get("verification"), dict) else None
    if ver:
        body = f"**{str(ver.get('status', '')).capitalize()}.** {str(ver.get('comments', '')).strip()}"
        extra = _bullets(ver.get("risks_identified") or [])
        out.append(_section("Architect check", body + ("\n\nRisks it raised:\n\n" + extra if extra else "")))

    if tasks:
        lines = []
        for i, t in enumerate(tasks):
            mark = "x" if i in done else " "
            lines.append(f"- [{mark}] **{str(t.get('title') or t.get('name') or f'Task {i + 1}').strip()}**")
            if str(t.get("description") or "").strip():
                lines.append(f"  {str(t['description']).strip()}")
            for c in t.get("acceptance_criteria") or []:
                lines.append(f"  - Done when: {str(c).strip()}")
        out.append(_section(f"Tasks ({len(done & set(range(len(tasks))))} of {len(tasks)} done)", "\n".join(lines)))

    cases = [c for c in plan.get("test_cases") or [] if isinstance(c, dict)]
    if cases:
        lines = []
        for c in cases:
            line = f"- **{str(c.get('title', '')).strip()}** ({str(c.get('type') or 'functionality')}): {str(c.get('expected', '')).strip()}"
            if c.get("tests"):
                line += f" Tests: {', '.join(f'`{t}`' for t in c['tests'])}"
            lines.append(line)
        out.append(_section("How it is tested", "\n".join(lines)))

    out.append(_section("What was built", _clip(str(job.get("builder_summary") or ""), 3000)))
    if changed_files:
        shown = changed_files[:40]
        out.append(_section("Files changed", _bullets([f"`{f}`" for f in shown]) + (f"\n- …and {len(changed_files) - 40} more" if len(changed_files) > 40 else "")))
    out.append(_section("Review", _clip(review, 4000)))
    refs = [f"- [{l.get('title') or l.get('ref')}]({l['url']})" if l.get("url") else f"- {l.get('title') or l.get('ref')}" for l in (links or []) if l.get("title") or l.get("ref")]
    out.append(_section("Linked tickets, errors and designs", "\n".join(refs)))
    text = "".join(out).rstrip() + "\n"
    if len(text) < 80 + len(head[0]):
        text += "\n_This job has not recorded anything more yet._\n"
    return text


# --------------------------------------------------------------------------- one feature


def feature_doc(feature: dict[str, Any], jobs: list[dict[str, Any]], names: dict[str, str] | None = None) -> str:
    """`jobs`: the web job summaries for this feature, each optionally carrying a `blurb` (its plan summary)."""
    names = names or {}
    out = [f"# {feature.get('name') or feature.get('id')}", ""]
    status = {"planned": "Planned", "in-progress": "In progress", "complete": "Complete"}.get(str(feature.get("status")), str(feature.get("status") or ""))
    out.append(f"- **Status:** {status}")
    if feature.get("depends_on"):
        out.append("- **Builds on:** " + ", ".join(names.get(d, d) for d in feature["depends_on"]))
    if feature.get("waiting_on"):
        out.append("- **Waiting on:** " + ", ".join(names.get(d, d) for d in feature["waiting_on"]))
    text = "\n".join(out) + "\n"
    text += _section("What it is", str(feature.get("summary") or ""))
    text += _section("Who and what it serves", str(feature.get("serves") or ""))
    paths = [str(p) for p in feature.get("paths") or []]
    text += _section("Where it lives in the code", _bullets([f"`{p}`" for p in paths]))
    if jobs:
        lines = []
        for j in jobs:
            state = (j.get("state") or {}).get("label") or STATUS_WORDS.get(str(j.get("status")), str(j.get("status")))
            lines.append(f"- **{j.get('title') or j.get('id')}** ({state})" + (f": {j['blurb']}" if j.get("blurb") else ""))
        done = sum(1 for j in jobs if (j.get("state") or {}).get("group") == "done")
        text += _section(f"Jobs ({done} of {len(jobs)} done)", "\n".join(lines))
    else:
        text += _section("Jobs", "_No jobs yet._")
    kpis = [k for k in feature.get("kpis") or [] if isinstance(k, dict)]
    if kpis:
        lines = []
        for k in kpis:
            st = k.get("status") if isinstance(k.get("status"), dict) else {}
            target = f" (target: {'at least' if k.get('direction') == 'up' else 'at most'} {k['target']}{(' ' + k['unit']) if k.get('unit') else ''})" if k.get("target") is not None else ""
            latest = st.get("latest") or {}
            lines.append(f"- **{k.get('name')}** (`{k.get('event')}`){target}: " + (f"latest {latest.get('value')}" if latest else "no result yet"))
        text += _section("How it is measured", "\n".join(lines))
    return text.rstrip() + "\n"


# --------------------------------------------------------------------------- the project's own files


def project_files(root: Path) -> list[dict[str, Any]]:
    """README, AI-helper notes and the markdown under docs/, as they are in the repository."""
    root = Path(root)
    found: list[Path] = [root / n for n in ROOT_NOTES if (root / n).is_file()]
    docs = root / "docs"
    if docs.is_dir():
        for f in sorted(docs.rglob("*.md")):
            rel = f.relative_to(root)
            if set(rel.parts) & SKIP_DIRS or f.name.startswith("."):
                continue
            found.append(f)
    out = []
    for f in found[:MAX_PROJECT_FILES]:
        rel = f.relative_to(root).as_posix()
        parent = Path(rel).parent.as_posix()
        out.append({"path": rel, "title": _file_title(f, rel), "size": f.stat().st_size, "group": "Project notes" if parent == "." else parent})
    return out


def _file_title(f: Path, rel: str) -> str:
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[:30]:
            m = re.match(r"^#\s+(.+)", line)
            if m:
                return m.group(1).strip()[:120]
    except OSError:
        pass
    return Path(rel).stem.replace("-", " ").replace("_", " ").strip().capitalize() or rel


def read_project_file(root: Path, rel: str) -> str:
    """One of the files `project_files` lists, and nothing else."""
    allowed = {f["path"] for f in project_files(root)}
    if rel not in allowed:
        raise DocsError("That file isn't part of the project documentation.")
    try:
        return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
    except OSError:
        raise DocsError("That file couldn't be read.")


# --------------------------------------------------------------------------- everything

def export_all(project_name: str, prd_text: str | None, features: list[tuple[dict[str, Any], str]], jobs: list[tuple[dict[str, Any], str]], files: list[tuple[str, str]]) -> str:
    """One markdown file: the product requirements, every feature, every job, and the project's own notes."""
    parts = [f"# {project_name}: project documentation\n\n_Generated from the live project. Product requirements first, then features, then jobs, then the project's own files._\n"]
    if prd_text and prd_text.strip():
        parts.append("\n---\n\n" + _demote(prd_text))
    for f, text in features:
        parts.append("\n---\n\n" + _demote(text))
    for j, text in jobs:
        parts.append("\n---\n\n" + _demote(text))
    for rel, text in files:
        parts.append(f"\n---\n\n## File: `{rel}`\n\n" + _demote(text, by=2))
    return "".join(parts).rstrip() + "\n"


def _demote(text: str, by: int = 1) -> str:
    """Push headings down so the pieces nest under the document's own title."""
    return re.sub(r"^(#{1,5})(\s)", lambda m: "#" * min(6, len(m.group(1)) + by) + m.group(2), text, flags=re.M)
