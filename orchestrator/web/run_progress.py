"""Summarize the worker's phase and LLM lifecycle messages for the live UI."""
from __future__ import annotations

import re


class RunProgress:
    # These are the headers emitted by common.print_phase, not arbitrary model prose.
    LABELS = {
        "PLANNING": "Planning implementation", "SCHEDULING": "Scheduling worker",
        "EXECUTION": "Starting execution", "GIT PREP": "Preparing git branch",
        "STATUS UPDATE": "Updating job status", "INVESTIGATION": "Investigating issue",
        "IMPLEMENTATION": "Implementing changes", "AI THINKING": "Agent thinking",
        "BUILDING": "Building project", "TESTING": "Running tests",
        "VERIFICATION": "Verifying changes", "REVIEW": "Reviewing changes",
        "PULL REQUEST": "Creating pull request", "DELIVERY": "Delivering build",
        "EXPORTING": "Exporting job", "CLEANUP": "Cleaning up", "COMPLETE": "Complete",
        "DEBUG LOOP": "Debugging issue", "COVERAGE": "Measuring code coverage",
        "MEASURING CODE COVERAGE": "Measuring code coverage",
    }
    TASK_PHASES = {"INVESTIGATION", "IMPLEMENTATION", "AI THINKING", "BUILDING", "TESTING", "VERIFICATION", "COVERAGE"}
    ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|[78])")

    def __init__(self) -> None:
        self._pending = b""
        self.label = ""
        self.task = ""
        self.step: int | None = None
        self.total: int | None = None
        self.models: list[str] = []
        self.active: list[str] = []

    def feed(self, chunk: bytes) -> None:
        # PTY reads can split a UTF-8 character or ANSI sequence. Decode complete lines only.
        lines = (self._pending + chunk).replace(b"\r", b"\n").split(b"\n")
        self._pending = lines.pop()[-16384:]
        for raw in lines:
            line = self.ANSI.sub(b"", raw).decode("utf-8", "replace").strip()
            bracket = re.fullmatch(r"\[(\d+)/(\d+)\]\s*(.*)", line)
            if bracket and 0 < int(bracket[1]) <= int(bracket[2]):
                self.step, self.total = int(bracket[1]), int(bracket[2])
                desc = bracket[3].strip()
                self.task = desc.capitalize() if desc else ""
                self.label = desc.capitalize() if desc else "Running step"
            header = re.fullmatch(r"=+\s*(.*?)\s*=+", line)
            if header:
                title = re.sub(r"^[^\w]+", "", header[1]).strip()
                task = re.fullmatch(r"(?:SUB[- ]TASK|STEP)\s*(\d+)/(\d+)(?::\s*(.*))?", title, re.I)
                if task and 0 < int(task[1]) <= int(task[2]):
                    self.step, self.total = int(task[1]), int(task[2])
                    desc = (task[3] or "").strip()
                    self.task = desc.capitalize() if desc else ""
                    self.label = desc.capitalize() if desc else "Running step"
                else:
                    phase, _, detail = title.partition(":")
                    phase = phase.strip().replace("_", " ").upper()
                    if phase in self.LABELS:
                        self.label = self.LABELS[phase]
                        if phase not in self.TASK_PHASES:
                            self.step = self.total = None
                            self.task = detail.strip().capitalize()

            start = re.fullmatch(r"- Running LLM \((.+), timeout=\d+s\)\.\.\.", line)
            if start:
                model = start[1]
                self.active.append(model)
                if model not in self.models:
                    self.models.append(model)
            elif re.fullmatch(r"\[output\] Received \d+ chars in [\d.]+s", line):
                if self.active:
                    self.active.pop(0)
            elif "Attempting fallback..." in line:
                for model in self.active:
                    if line.startswith(f"⚠️  {model} timed out.") or line.startswith(f"⚠️  {model} failed ("):
                        self.active.remove(model)
                        break

    def snapshot(self, running: bool = True) -> dict:
        active = self.active if running else []
        return {"label": self.label, "task": self.task, "step": self.step, "total": self.total,
                "models": list(self.models), "active_models": list(dict.fromkeys(active)), "agents": len(active)}
