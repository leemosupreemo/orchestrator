"""A job and its runs: what each run was for, all of a job's runs in one list, and what they mean for the job.

A *job* is a piece of work (plan, build, verify, document, finalize) kept in `.orchestrator/jobs/<id>.json`. A *run* is one
process doing part of it, started from the app: planning, a build pass, one fix attempt, a merge. A small job is often a
single run end to end (1:1); a larger one has a run per stage, several fix attempts, and so on.

The app keeps live runs in memory and drops finished ones after a while, and a restart forgets them, so a run's link to its
job used to vanish. `record` writes a small sidecar beside each run's log (`logs/ui/<run id>.json`) when the run ends;
`for_job` merges those with the live runs, labels each one by what it did ("Build", "Fix attempt 2"), and `relationship`
says what the runs add up to for the job: one run or several, which run is working now and on what step, how many runs each
stage took, and a failed run the job record doesn't reflect yet (a crash before the worker could save the job).

Pure functions over the runtime folder and run summaries; nothing here starts or stops a run.
"""
from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

SIDECAR_DIR = ("logs", "ui")
MAX_SCAN = 400
LEGACY_TASK_RUN_LIMIT = 24
TASK_MARKER = "[task-run] "
TASK_ATTEMPT_MARKER = "[task-attempt] "
TASK_ID = re.compile(r"[0-9a-f]{32}")
LEGACY_TASK_HEADER = re.compile(r"^=+\s*[^\w\n]*SUB-TASK\s+\d+(?:-\d+)?/\d+\s*:\s*(.+?)\s*=+\s*$", re.I | re.M)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

PHASES = ("plan", "build", "verify", "document", "finalize")
PHASE_TITLES = {"plan": "Plan", "build": "Build", "verify": "Verify", "document": "Document", "finalize": "Finalize"}

# What each run of a job is for: (label, the job stage it belongs to). None marks a side errand that isn't work on the job.
KINDS: dict[str, tuple[str, str | None]] = {
    "new_job": ("Planning", "plan"),
    "fix": ("Planning", "plan"),
    "revise": ("Re-planning", "plan"),
    "answer": ("Planning", "plan"),
    "plan_epic": ("Planning", "plan"),
    "approve": ("Approval", "plan"),
    "splinter": ("Splitting into jobs", "plan"),
    "schedule": ("Build", "build"),
    "execute": ("Build", "build"),
    "resume": ("Build", "build"),
    "debug": ("Fix attempt", "verify"),
    "visual_check": ("Screen check", "verify"),
    "ux_review_job": ("UX check", "verify"),
    "deliver": ("Delivery to testers", "finalize"),
    "merge": ("Merge", "finalize"),
    "complete": ("Completion", "finalize"),
    "discard": ("Discard", "finalize"),
    "delete_job": ("Delete", "finalize"),
    "export_job": ("Export", None),
    "web_preview": ("Preview link", None),
    "dev_preview": ("Live preview", None),
}
NUMBERED = {"debug", "resume", "execute", "schedule", "revise"}  # kinds that repeat, so each gets its number


def kind(action: str) -> tuple[str, str | None]:
    return KINDS.get(action, (action.replace("_", " ").capitalize() or "Run", None))


def _dir(runtime: Path) -> Path:
    return runtime.joinpath(*SIDECAR_DIR)


def record(runtime: Path, run: dict[str, Any]) -> Path | None:
    """Keep what a finished run did and which job it was for, beside its log, so the job still lists it after a restart.
    `run` is a run summary (the shape the run cards use). Runs that belong to no job aren't recorded."""
    job = run.get("job") or run.get("result_job")
    if not job or not run.get("id"):
        return None
    result = run.get("result") or {}
    data = {"id": run["id"], "action": run.get("action", ""), "title": run.get("title", ""), "job": job,
            "started": run.get("started"), "ended": run.get("ended"), "exit_code": run.get("exit_code"), "stopped": bool(run.get("stopped")),
            "headline": result.get("headline", "") if isinstance(result, dict) else "", "tone": result.get("tone", "") if isinstance(result, dict) else "",
            "log": f"{'/'.join(SIDECAR_DIR)}/{run['id']}-{run.get('action', '')}.log"}
    if run.get("task_refs"):
        data["task_refs"] = run["task_refs"]
    if run.get("task_attempts"):
        data["task_attempts"] = run["task_attempts"]
    if run.get("running") and isinstance(run.get("pid"), int):
        data["running"] = True
        data["pid"] = run["pid"]
    folder = _dir(runtime)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{run['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def recorded(runtime: Path, job_id: str | None = None) -> list[dict[str, Any]]:
    """Recorded runs, newest first (all, or one job's)."""
    folder = _dir(runtime)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:MAX_SCAN]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") and (job_id is None or data.get("job") == job_id):
            out.append(data)
    return out


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def status(run: dict[str, Any]) -> str:
    if run.get("running") and _pid_alive(run.get("pid")):
        return "waiting" if run.get("waiting") else "running"
    if run.get("stopped"):
        return "stopped"
    code = run.get("exit_code")
    if code is None:
        return "unknown"  # the app stopped while it ran, so how it ended wasn't seen
    return "succeeded" if code == 0 else "failed"


def for_job(runtime: Path, job_id: str, live: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    """Every run of one job, live and recorded, newest first, each labelled by what it did and numbered where it repeats
    ("Fix attempt 3"). Live runs win over their own records."""
    now = now or time.time()
    by_id: dict[str, dict[str, Any]] = {r["id"]: {**r, "live": False} for r in recorded(runtime, job_id)}
    for r in live:
        if job_id in (r.get("job"), r.get("result_job")):
            result = r.get("result") or {}
            previous = by_id.get(r["id"], {})
            by_id[r["id"]] = {**previous, **r, "live": True,
                              "task_refs": r.get("task_refs") or previous.get("task_refs", {}),
                              "task_attempts": r.get("task_attempts") or previous.get("task_attempts", {}),
                              "headline": (result.get("headline") if isinstance(result, dict) else "") or by_id.get(r["id"], {}).get("headline", ""),
                              "tone": (result.get("tone") if isinstance(result, dict) else "") or by_id.get(r["id"], {}).get("tone", "")}
    runs = sorted(by_id.values(), key=lambda r: r.get("started") or 0)
    counts: dict[str, int] = {}
    totals: dict[str, int] = {}
    for r in runs:
        label, _ = kind(r.get("action", ""))
        totals[label] = totals.get(label, 0) + 1
    out = []
    for n, r in enumerate(runs, 1):
        action = r.get("action", "")
        label, phase = kind(action)
        counts[label] = counts.get(label, 0) + 1
        numbered = action in NUMBERED and totals[label] > 1
        started = r.get("started")
        end = r.get("ended") or (now if r.get("running") else None)
        seconds = round(end - started) if started and end else None
        out.append({**r, "kind": label, "label": f"{label} {counts[label]}" if numbered else label, "phase": phase,
                    "side": phase is None, "status": status(r), "seconds": seconds, "number": n})
    return list(reversed(out))


def ensure_task_ids(tasks: list[dict[str, Any]]) -> bool:
    """Keep an ID on each task so moving or renaming it preserves run links."""
    changed = False
    seen: set[str] = set()
    for task in tasks:
        task_id = task.get("run_history_id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or task_id in seen:
            task_id = uuid4().hex
            task["run_history_id"] = task_id
            changed = True
        seen.add(task_id)
    return changed


def task_marker(job_id: str, tasks: list[dict[str, Any]]) -> str:
    return TASK_MARKER + json.dumps({"job": job_id, "ids": [task["run_history_id"] for task in tasks]})


def parse_task_marker(line: str) -> tuple[str, set[str]] | None:
    if not line.startswith(TASK_MARKER):
        return None
    try:
        payload = json.loads(line[len(TASK_MARKER):])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("job"), str) or not isinstance(payload.get("ids"), list):
        return None
    ids = {value for value in payload["ids"] if isinstance(value, str) and TASK_ID.fullmatch(value)}
    return (payload["job"], ids) if ids else None


def task_attempt_marker(job_id: str, task: dict, status: str, detail: str) -> str:
    return TASK_ATTEMPT_MARKER + json.dumps({"job": job_id, "id": task["run_history_id"],
                                             "status": status, "detail": detail[:500]})


def parse_task_attempt(line: str) -> tuple[str, str, dict] | None:
    if not line.startswith(TASK_ATTEMPT_MARKER):
        return None
    try:
        payload = json.loads(line[len(TASK_ATTEMPT_MARKER):])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    job, task_id, status, detail = (payload.get(key) for key in ("job", "id", "status", "detail"))
    if not all(isinstance(value, str) for value in (job, task_id, status, detail)):
        return None
    if not TASK_ID.fullmatch(task_id) or status not in ("running", "passed", "incomplete", "failed", "paused"):
        return None
    return job, task_id, {"status": status, "detail": detail[:500]}


def _title(value: str) -> str:
    return " ".join(value.split()).casefold()


@lru_cache(maxsize=400)
def _legacy_task_titles(path: Path, modified: int, size: int) -> tuple[str, ...]:
    try:
        with path.open("rb") as stream:
            contents = stream.read(512 * 1024)
            if size > len(contents):
                stream.seek(max(len(contents), size - 512 * 1024))
                contents += b"\n" + stream.read(512 * 1024)
    except OSError:
        return ()
    text = ANSI_ESCAPE.sub("", contents.decode("utf-8", errors="replace")).replace("\r", "\n")
    return tuple(_title(heading) for heading in LEGACY_TASK_HEADER.findall(text))


def task_runs(runtime: Path, job_id: str, tasks: list[Any], runs: list[dict[str, Any]], *,
              current_task_index: int | None = None) -> list[list[dict[str, Any]]]:
    """Group runs by durable task ID, with exact title matches for old transcripts and active-task fallback."""
    groups: list[list[dict[str, Any]]] = [[] for _ in tasks]
    ids = {task["run_history_id"]: index for index, task in enumerate(tasks)
           if isinstance(task, dict) and isinstance(task.get("run_history_id"), str)}
    titles: dict[str, list[int]] = {}
    for index, task in enumerate(tasks):
        title = task if isinstance(task, str) else (task.get("title") or task.get("name")) if isinstance(task, dict) else ""
        if isinstance(title, str) and title.strip():
            titles.setdefault(_title(title), []).append(index)
    if not groups:
        return groups
    legacy_reads = 0
    for run in runs:
        refs = run.get("task_refs") or {}
        matched = set()
        if isinstance(refs, dict) and refs:
            references = refs.get(job_id)
            if isinstance(references, list):
                matched.update(ids[ref] for ref in references if isinstance(ref, str) and ref in ids)
        elif (legacy_reads < LEGACY_TASK_RUN_LIMIT and not run.get("running")
              and run.get("action") in {"approve", "new_job", "schedule", "execute", "resume", "debug"}):
            run_id, action = run["id"], run["action"]
            if isinstance(run_id, str) and re.fullmatch(r"[\w-]+", run_id):
                path = _dir(runtime) / f"{run_id}-{action}.log"
                if not path.is_symlink():
                    try:
                        info = path.stat()
                    except OSError:
                        info = None
                    if info is not None:
                        legacy_reads += 1
                        for heading in _legacy_task_titles(path, info.st_mtime_ns, info.st_size):
                            names = [heading] if heading in titles else heading.split(" + ")
                            if all(len(titles.get(name, ())) == 1 for name in names):
                                matched.update(titles[name][0] for name in names)
        if (not matched and current_task_index is not None and 0 <= current_task_index < len(groups)
                and not run.get("side") and run.get("action") in {"schedule", "execute", "resume", "debug"}):
            matched.add(current_task_index)
        for index in matched:
            groups[index].append(run)
    return groups


def relationship(runs: list[dict[str, Any]], job: dict[str, Any]) -> dict[str, Any]:
    """What a job's runs add up to: 1:1 or several, the run working now, runs per stage, and a failure to bring to the job.
    `runs` is `for_job`'s list (newest first); `job` the job record."""
    work = [r for r in runs if not r.get("side")]
    active = next((r for r in work if r["status"] in ("running", "waiting")), None)
    per_phase = {p: sum(1 for r in work if r.get("phase") == p) for p in PHASES}
    latest = work[0] if work else None
    try:
        from datetime import datetime
        job_updated = datetime.fromisoformat(str(job.get("updated_at"))).timestamp() if job.get("updated_at") else 0.0
    except ValueError:
        job_updated = 0.0
    # A run that failed or vanished after the job was last saved: the worker didn't get to say what happened.
    unreported = None
    if latest and not active and latest["status"] in ("failed", "unknown") and (latest.get("ended") or latest.get("started") or 0) >= job_updated - 1:
        unreported = {"id": latest["id"], "label": latest["label"], "status": latest["status"], "headline": latest.get("headline", "")}
    return {"count": len(work), "single": len(work) <= 1, "active": active and {"id": active["id"], "label": active["label"],
            "phase": active.get("phase"), "status": active["status"], "now": active.get("now", ""), "seconds": active.get("seconds")},
            "per_phase": per_phase, "latest": latest and {"id": latest["id"], "label": latest["label"], "status": latest["status"]},
            "unreported": unreported}


COHESIVE_COMPLEXITIES = {"small", "low", "trivial"}
SINGLE_PASS_TYPES = {"bug-fix", "bug-investigate", "test-audit", "coverage", "feature-design", "design", "quick-fix"}
FOUNDATION_WORDS = (
    "schema", "migration", "database", "data model", "data layer", "models", "entities",
    "persistence", "repository", "types", "protocol", "interfaces", "scaffold", "boilerplate",
    "skeleton", "project setup", "infrastructure", "dependencies", "plumbing",
)
USER_FACING_WORDS = (
    "screen", "view", "page", "button", "form", "command", "cli", "ui", "ux", "flow",
    "user can", "users can", "can now", "end-to-end", "end to end", "displays", "shows",
    "visible", "launch", "runs", "menu", "tab", "dialog", "output",
)


def _norm_files(files: Any) -> list[str]:
    if not isinstance(files, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in files:
        val = str(raw or "").strip().removeprefix("./")
        if val and not val.endswith("/") and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _is_foundation_task(task: Any) -> bool:
    if not isinstance(task, dict):
        return False
    if task.get("foundation") is True:
        return True
    parts = [str(task.get("title", "")), str(task.get("description", ""))] + [str(a) for a in task.get("acceptance_criteria") or []]
    text = " ".join(parts).lower()
    has_found = any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in FOUNDATION_WORDS)
    has_user = any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in USER_FACING_WORDS)
    return has_found and not has_user


def cohesive_batch_indices(tasks: list[Any], completed: list[int] | set[int]) -> list[int]:
    """Contiguous uncompleted task indices (2-3 tasks) that can execute in one combined builder call."""
    if not isinstance(tasks, list) or len(tasks) < 2:
        return []
    done = {int(i) for i in (completed or []) if str(i).isdigit()}
    remaining = [i for i in range(len(tasks)) if i not in done]
    if not (2 <= len(remaining) <= 3):
        return []
    if remaining != list(range(remaining[0], remaining[0] + len(remaining))):
        return []
    all_files: set[str] = set()
    prev_files: set[str] = set()
    for pos, idx in enumerate(remaining):
        t = tasks[idx]
        if not isinstance(t, dict):
            return []
        comp = str(t.get("complexity") or "").strip().lower()
        if comp not in COHESIVE_COMPLEXITIES:
            return []
        t_files = set(_norm_files(t.get("likely_files")))
        if not t_files:
            return []
        if pos > 0 and not (prev_files & t_files):
            return []
        prev_files = t_files
        all_files.update(t_files)
    if len(all_files) > 4:
        return []
    return remaining


def _resolve_explicit_deps(task: Any, index: int, tasks: list[Any]) -> list[int]:
    """Resolve a task's explicit `depends_on` references (1-based index, T<n>, run_history_id, or title) to 0-based task indices."""
    if not isinstance(task, dict):
        return []
    raw_deps = task.get("depends_on")
    if not isinstance(raw_deps, list):
        return []
    by_id = {
        str(t.get("run_history_id")): idx
        for idx, t in enumerate(tasks)
        if isinstance(t, dict) and t.get("run_history_id")
    }
    by_title = {
        _title(str(t.get("title") or t.get("name") or "")): idx
        for idx, t in enumerate(tasks)
        if isinstance(t, dict) and (t.get("title") or t.get("name"))
    }
    resolved: list[int] = []
    for ref in raw_deps:
        target: int | None = None
        if isinstance(ref, int) and not isinstance(ref, bool):
            target = ref - 1 if 1 <= ref <= len(tasks) else (ref if 0 <= ref < len(tasks) else None)
        elif isinstance(ref, str):
            s = ref.strip()
            m = re.fullmatch(r"(?:[tT]|task\s*)?(\d+)", s, re.I)
            if m:
                n = int(m.group(1))
                target = n - 1 if 1 <= n <= len(tasks) else None
            elif s in by_id:
                target = by_id[s]
            elif _title(s) in by_title:
                target = by_title[_title(s)]
        if target is not None and 0 <= target < len(tasks) and target != index and target not in resolved:
            resolved.append(target)
    return resolved


def breakdown(job: dict[str, Any]) -> dict[str, Any]:
    """Dynamically determine how a job breaks down into runs:
    - `single_run`: taskless jobs (bug-fix, investigate, test-audit, design), quick-fix, 1-task feature-plan, or a 2-3 task cohesive batch.
    - `sequential_runs`: multi-task feature-plan whose tasks share files, depend on each other linearly, or run on a shared branch.
    - `parallel_runs`: multi-task feature-plan with >= 2 tasks in the same dependency wave that touch disjoint likely_files.
    """
    if not isinstance(job, dict):
        job = {}
    job_type = str(job.get("type") or "")
    if not job_type:
        if job.get("source") == "manual" or "repro_steps" in (job.get("plan") or {}):
            job_type = "bug-fix"
        elif job.get("source") == "quick" or "quick" in str(job.get("job_id") or ""):
            job_type = "quick-fix"
        else:
            job_type = "feature-plan"
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    raw_tasks = [t for t in (plan.get("tasks") or []) if t] if isinstance(plan.get("tasks"), list) else []
    completed = {int(i) for i in (job.get("completed_task_indices") or []) if str(i).isdigit()}
    branch_mode = str(job.get("branch_mode") or "new").lower()
    shared_branch = "current" in branch_mode or "manual" in branch_mode
    job_done = str(job.get("status") or "") in ("completed", "review-needed")

    if job_type in SINGLE_PASS_TYPES or not raw_tasks:
        files = _norm_files(plan.get("likely_files"))
        for t in raw_tasks:
            if isinstance(t, dict):
                for f in _norm_files(t.get("likely_files")):
                    if f not in files:
                        files.append(f)
        task_ids = [str(t["run_history_id"]) for t in raw_tasks if isinstance(t, dict) and isinstance(t.get("run_history_id"), str)]
        all_done = job_done or (bool(raw_tasks) and all(i in completed for i in range(len(raw_tasks))))
        reason = (
            "Quick change builds all tasks together in one pass."
            if job_type == "quick-fix" and len(raw_tasks) > 1
            else "Single-pass job without separate task runs."
        )
        return {
            "mode": "single_run",
            "label": "1 run",
            "reason": reason,
            "parallel": False,
            "planned_runs": 1,
            "waves": 1,
            "groups": [{
                "group_id": "MAIN",
                "title": str(job.get("title") or plan.get("title") or "Build"),
                "task_indices": list(range(len(raw_tasks))),
                "task_ids": task_ids,
                "likely_files": files,
                "depends_on": [],
                "wave": 0,
                "parallel": False,
                "cohesive_batch": len(raw_tasks) > 1,
                "status": "completed" if all_done else "pending",
            }],
        }

    if len(raw_tasks) == 1:
        t0 = raw_tasks[0] if isinstance(raw_tasks[0], dict) else {"title": str(raw_tasks[0])}
        tid = [str(t0["run_history_id"])] if isinstance(t0.get("run_history_id"), str) else []
        return {
            "mode": "single_run",
            "label": "1 run",
            "reason": "Single task runs in one build pass.",
            "parallel": False,
            "planned_runs": 1,
            "waves": 1,
            "groups": [{
                "group_id": "T1",
                "title": str(t0.get("title") or t0.get("name") or "Task 1"),
                "task_indices": [0],
                "task_ids": tid,
                "likely_files": _norm_files(t0.get("likely_files") or plan.get("likely_files")),
                "depends_on": [],
                "wave": 0,
                "parallel": False,
                "cohesive_batch": False,
                "status": "completed" if (0 in completed or job_done) else "pending",
            }],
        }

    if not job.get("retry_tasks_individually"):
        full_batch = cohesive_batch_indices(raw_tasks, set())
        if full_batch == list(range(len(raw_tasks))):
            titles = [str((raw_tasks[i].get("title") if isinstance(raw_tasks[i], dict) else raw_tasks[i]) or f"Task {i + 1}") for i in full_batch]
            files: list[str] = []
            task_ids: list[str] = []
            for i in full_batch:
                t = raw_tasks[i]
                if isinstance(t, dict):
                    if isinstance(t.get("run_history_id"), str):
                        task_ids.append(t["run_history_id"])
                    for f in _norm_files(t.get("likely_files")):
                        if f not in files:
                            files.append(f)
            all_done = job_done or all(i in completed for i in full_batch)
            return {
                "mode": "single_run",
                "label": "1 batched run",
                "reason": f"{len(raw_tasks)} small tasks share files and run together in one pass.",
                "parallel": False,
                "planned_runs": 1,
                "waves": 1,
                "groups": [{
                    "group_id": f"T1-T{len(raw_tasks)}",
                    "title": " + ".join(titles),
                    "task_indices": full_batch,
                    "task_ids": task_ids,
                    "likely_files": files,
                    "depends_on": [],
                    "wave": 0,
                    "parallel": False,
                    "cohesive_batch": True,
                    "status": "completed" if all_done else "pending",
                }],
            }

    # Build per-task groups and infer dependencies from explicit depends_on, file overlaps, foundation slice, and branch mode.
    n = len(raw_tasks)
    files_by_idx = [_norm_files(t.get("likely_files")) if isinstance(t, dict) else [] for t in raw_tasks]
    explicit_by_idx = [_resolve_explicit_deps(t, i, raw_tasks) for i, t in enumerate(raw_tasks)]
    any_explicit = any(bool(d) for d in explicit_by_idx)
    first_is_foundation = _is_foundation_task(raw_tasks[0])

    deps_by_idx: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for dep in explicit_by_idx[i]:
            if dep != i:
                deps_by_idx[i].add(dep)
        if shared_branch and i > 0:
            deps_by_idx[i].add(i - 1)
            continue
        my_files = set(files_by_idx[i])
        for prev in range(i):
            prev_files = set(files_by_idx[prev])
            if not my_files or not prev_files or (my_files & prev_files):
                deps_by_idx[i].add(prev)
        if i > 0 and first_is_foundation and not any_explicit:
            deps_by_idx[i].add(0)

    # Break any accidental cycle by keeping only dependencies with index < i when a cycle exists
    waves_by_idx = [0] * n
    visiting: set[int] = set()
    computed: dict[int, int] = {}

    def compute_wave(idx: int) -> int:
        if idx in computed:
            return computed[idx]
        if idx in visiting:
            return 0
        visiting.add(idx)
        valid_deps = [d for d in sorted(deps_by_idx[idx]) if 0 <= d < n and d != idx]
        w = 0 if not valid_deps else 1 + max(compute_wave(d) for d in valid_deps)
        visiting.remove(idx)
        computed[idx] = w
        return w

    for i in range(n):
        waves_by_idx[i] = compute_wave(i)

    # Transitive reduction of depends_on for clean display
    reduced_deps: list[list[int]] = []
    for i in range(n):
        direct = sorted(d for d in deps_by_idx[i] if 0 <= d < n and d != i and waves_by_idx[d] < waves_by_idx[i])
        ancestors_of_direct: set[int] = set()
        stack = list(direct)
        while stack:
            cur = stack.pop()
            for parent in deps_by_idx[cur]:
                if parent not in ancestors_of_direct and parent != cur:
                    ancestors_of_direct.add(parent)
                    stack.append(parent)
        reduced_deps.append([d for d in direct if d not in ancestors_of_direct])

    wave_counts: dict[int, int] = {}
    for w in waves_by_idx:
        wave_counts[w] = wave_counts.get(w, 0) + 1

    groups: list[dict[str, Any]] = []
    for i, t in enumerate(raw_tasks):
        t_dict = t if isinstance(t, dict) else {"title": str(t)}
        tid = [str(t_dict["run_history_id"])] if isinstance(t_dict.get("run_history_id"), str) else []
        w = waves_by_idx[i]
        groups.append({
            "group_id": f"T{i + 1}",
            "title": str(t_dict.get("title") or t_dict.get("name") or f"Task {i + 1}"),
            "task_indices": [i],
            "task_ids": tid,
            "likely_files": files_by_idx[i],
            "depends_on": [f"T{d + 1}" for d in reduced_deps[i]],
            "wave": w,
            "parallel": wave_counts.get(w, 0) > 1,
            "cohesive_batch": False,
            "status": "completed" if (i in completed or job_done) else "pending",
        })

    total_waves = (max(waves_by_idx) + 1) if waves_by_idx else 1
    has_parallel = any(count > 1 for count in wave_counts.values())
    if has_parallel:
        mode = "parallel_runs"
        label = f"{n} runs · {total_waves} wave{'s' if total_waves != 1 else ''} (parallel)"
        reason = f"Independent tasks touch non-overlapping files and run concurrently across {total_waves} wave{'s' if total_waves != 1 else ''}."
    else:
        mode = "sequential_runs"
        label = f"{n} sequential runs"
        reason = (
            "Current-branch mode runs tasks sequentially in the project folder."
            if shared_branch
            else "Tasks share files or build on earlier tasks, so they run one after another."
        )

    return {
        "mode": mode,
        "label": label,
        "reason": reason,
        "parallel": has_parallel,
        "planned_runs": len(groups),
        "waves": total_waves,
        "groups": groups,
    }
