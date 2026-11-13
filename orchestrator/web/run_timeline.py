"""A run's output as a timeline of steps, for the run page.

The worker prints a lot: phase headers (`===== PLANNING =====`), numbered steps (`[2/4] Updating job status...`), model
calls (`Running LLM (claude-sonnet-4-6, timeout=600s)` ... `[output] Received 5120 chars in 34.2s`), and everything the
tools it runs print in between. Read top to bottom that's a wall of terminal text. This turns it into the few steps a
person cares about, each with a plain title, how long it took, whether it worked, the model calls made during it, and
its own output to open when they want the detail.

It only reads lines the worker prints on purpose (the same ones RunProgress reads); anything else is output that belongs
to the step it appears in. Fed as the output arrives, so the page follows a live run; or fed a saved log afterwards, when
there are no times to tell.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

from orchestrator.web.run_progress import RunProgress

MAX_STEPS = 200
MAX_LINES_PER_STEP = 400
MAX_LINE_CHARS = 400

ANSI = RunProgress.ANSI
# What kind of work a step is, for its icon: the first that matches its title wins.
KINDS = [
    ("plan", r"\bplan|architect|design|investigat|research"),
    ("review", r"\breview|pull request"),
    ("git", r"\bgit\b|branch|commit|merge|push|pull\b"),
    ("test", r"\btest|coverage|verif|check"),
    ("build", r"\bbuild|compil|archiv|install|deliver|distribut"),
    ("model", r"\bwaiting for|thinking|agent"),
    ("code", r"\bimplement|writ|chang|fix|task|step"),
]
FAILURE = re.compile(r"^(?:❌|✗|✘|Error:|ERROR\b|FAILED\b|Traceback \(most recent call last\))|\bfatal:", re.I)


def kind_of(title: str) -> str:
    lowered = title.lower()
    return next((kind for kind, pattern in KINDS if re.search(pattern, lowered)), "run")


def plain_title(text: str) -> str:
    """"Preparing git branch: ai/job-3-login..." → "Preparing git branch: ai/job-3-login"; first letter capital."""
    text = re.sub(r"\s+", " ", text).strip().rstrip(".…").strip()
    if sum(c.isupper() for c in text) > max(3, sum(c.islower() for c in text)):
        text = text.lower()  # the worker shouts its headers: "ADD LOGIN PAGE" reads as "Add login page"
    return (text[:1].upper() + text[1:])[:160] if text else ""


class RunTimeline:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._pending = b""
        self._next_id = 0
        self.steps: list[dict[str, Any]] = []
        self._lines: dict[int, deque] = {}
        self._calls: list[dict[str, Any]] = []  # model calls waiting for an answer, oldest first
        self._in_runner = False                  # inside a build or test run, whose own [i/n] counters aren't steps
        self._worker = (0, 0)                    # the worker's own [n/total] count, which still counts inside a runner
        self._open("Starting", timed=True)

    # ------------------------------------------------------------------ steps

    def _open(self, title: str, timed: bool = True, of: str = "") -> dict[str, Any]:
        now = self._clock() if timed else None
        if self.steps:
            last = self.steps[-1]
            if last["status"] == "running":  # the run moved on, so this step finished; error lines in it show as a count
                last["status"] = "done"
                last["ended"] = now
            if last["title"] == "Starting" and not self._lines.get(last["id"]):
                self.steps.pop()  # nothing happened before the first real step
        step = {"id": self._next_id, "title": title, "of": of, "kind": kind_of(title), "status": "running",
                "started": now, "ended": None, "events": [], "errors": 0, "lines": 0, "last": ""}
        self._next_id += 1
        self.steps.append(step)
        self._lines[step["id"]] = deque(maxlen=MAX_LINES_PER_STEP)
        while len(self.steps) > MAX_STEPS:
            gone = self.steps.pop(0)
            self._lines.pop(gone["id"], None)
        return step

    @property
    def current(self) -> dict[str, Any]:
        return self.steps[-1]

    # ------------------------------------------------------------------ reading output

    def feed(self, chunk: bytes, timed: bool = True) -> None:
        lines = (self._pending + chunk).replace(b"\r", b"\n").split(b"\n")
        self._pending = lines.pop()[-16384:]
        for raw in lines:
            self.line(ANSI.sub(b"", raw).decode("utf-8", "replace").rstrip(), timed)

    def line(self, text: str, timed: bool = True) -> None:
        line = text.strip()
        if not line:
            return
        header = re.fullmatch(r"=+\s*(.*?)\s*=+", line)
        if header:
            self._header(re.sub(r"^[^\w]+", "", header[1]).strip(), timed)
            return
        numbered = re.fullmatch(r"\[(\d+)/(\d+)\]\s*(.+)", line)
        n, total = (int(numbered[1]), int(numbered[2])) if numbered else (0, 0)
        worker_step = numbered and 0 < n <= total and (
            not self._in_runner or (total == self._worker[1] and n > self._worker[0]))
        if worker_step:
            self._worker, self._in_runner = (n, total), False
            title = plain_title(numbered[3])
            step = self.current
            if title and title != step["title"]:
                if not step["lines"] and not step["events"]:
                    step.update(title=title, kind=kind_of(title), of=f"{n} of {total}")  # the header just before said it less exactly
                else:
                    self._open(title, timed, of=f"{n} of {total}")
            return
        self._model_event(line, timed)
        step = self.current
        self._lines[step["id"]].append(text[:MAX_LINE_CHARS])
        step["lines"] += 1
        if FAILURE.search(line):
            step["errors"] += 1
        if not re.search(r"Running LLM|\[output\] Received|^<<<|Attempting fallback", line):
            step["last"] = re.sub(r"^[-•*]\s+", "", line)[:200]  # what it's doing now, for the step in progress

    def _header(self, title: str, timed: bool) -> None:
        task = re.fullmatch(r"(?:SUB[- ]TASK|STEP)\s*(\d+)/(\d+)(?::\s*(.*))?", title, re.I)
        if task:
            name = plain_title(task[3] or "") or f"Step {task[1]}"
            self._in_runner = False
            self._open(name, timed, of=f"{task[1]} of {task[2]}")["kind"] = "code"  # a task of the plan: writing code
            return
        phase, _, detail = title.partition(":")
        key = phase.strip().replace("_", " ").upper()
        label = RunProgress.LABELS.get(key) or plain_title(phase.lower())
        if not label:
            return
        self._in_runner = key in {"BUILDING", "TESTING", "COVERAGE", "MEASURING CODE COVERAGE"}
        if key == "COMPLETE":
            self.finish(0, timed)
            return
        name = f"{label}: {plain_title(detail)}" if detail.strip() else label
        if name != self.current["title"]:
            self._open(plain_title(name), timed)

    def _model_event(self, line: str, timed: bool) -> None:
        now = self._clock() if timed else None
        start = re.search(r"Running LLM \(([^,)]+)", line)
        if start:
            event = {"model": start[1].strip(), "status": "running", "started": now, "seconds": None, "reason": ""}
            self.current["events"].append(event)
            self._calls.append(event)
            return
        done = re.search(r"\[output\] Received (\d+) chars in ([\d.]+)s", line)
        if done and self._calls:
            event = self._calls.pop(0)
            event.update(status="done", seconds=round(float(done[2])))
            return
        failed = re.match(r"⚠️\s+(\S+) (?:failed \((.*)|timed out)", line)
        if failed:
            event = next((e for e in self._calls if e["model"] == failed[1]), None)
            reason = RunProgress._reason(failed[2] or "", line)
            if event:
                self._calls.remove(event)
                event.update(status="failed", reason=reason, seconds=round(now - event["started"]) if now and event["started"] else None)
            else:
                self.current["events"].append({"model": failed[1], "status": "failed", "started": now, "seconds": None, "reason": reason})

    def finish(self, exit_code: int | None, timed: bool = True) -> None:
        """The run ended: the last step is done, or failed when the run did."""
        if not self.steps:
            return
        step = self.current
        if step["status"] == "running":
            step["status"] = "failed" if exit_code not in (0, None) else "done"
            step["ended"] = self._clock() if timed else None
        elif exit_code not in (0, None) and not any(s["status"] == "failed" for s in self.steps):
            step["status"] = "failed"  # it said it was done, then the run failed after all
        for event in self._calls:
            event["status"] = "failed" if exit_code else "done"
        self._calls = []

    # ------------------------------------------------------------------ for the page

    def view(self, running: bool = True) -> dict[str, Any]:
        now = self._clock()
        steps = []
        for s in self.steps:
            if s["title"] == "Starting" and s["lines"] <= 2 and len(self.steps) > 1:
                continue  # just the "Context: …" line before the first real step
            seconds = None
            if s["started"] is not None:
                seconds = round((s["ended"] or (now if running else s["started"])) - s["started"])
            events = [{**{k: e[k] for k in ("model", "status", "reason")},
                       "seconds": e["seconds"] if e["seconds"] is not None or not e["started"] else round(now - e["started"])}
                      for e in s["events"]]
            steps.append({"id": s["id"], "title": s["title"], "of": s["of"], "kind": s["kind"], "status": s["status"],
                          "seconds": seconds, "events": events, "lines": s["lines"], "errors": s["errors"], "last": s["last"]})
        return {"steps": steps, "running": running}

    def lines(self, step_id: int) -> list[str]:
        return list(self._lines.get(step_id, []))


def from_log(text: str, exit_code: int | None = None) -> RunTimeline:
    """A saved run's timeline, from its log (no times: the log doesn't record when each line came)."""
    timeline = RunTimeline()
    timeline.steps[0]["started"] = None
    for line in text.splitlines():
        if re.fullmatch(r"\[exited(?: with code (\d+))?\]", line.strip()):
            match = re.search(r"code (\d+)", line)
            exit_code = int(match[1]) if match else 0
            continue
        timeline.line(ANSI.sub(b"", line.encode()).decode("utf-8", "replace"), timed=False)
    timeline.finish(exit_code, timed=False)
    return timeline
