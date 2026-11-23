"""Summarize the worker's phase and LLM lifecycle messages for the live UI."""
from __future__ import annotations

import json
import re
import time

MODEL_QUOTA = re.compile(r"⏸️\s+(\S+) (?:is (out of (?:usage|requests for now)[^.]*?)|ran out of usage)\.")


_ACTIVITY_NOISE_RE = re.compile(
    r"^(?:"
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*(?:Thinking|Working|Waiting)|"
    r"Thinking\s*\(Active stderr\)|"
    r"\[waiting\]|"
    r"\[Stall Notice\]|"
    r"💡\s*\[Duration Notice\]|"
    r"👉\s*If it appears stuck|"
    r"hook:\s*(?:PreToolUse|PostToolUse|SessionStart|Stop)\b|"
    r"Running LLM\b|"
    r"\[output\]\s*Received\b|"
    r"⏸️\s+\S+ (?:is out of|ran out of)\b|"
    r"<<<|"
    r"Attempting fallback|"
    r"exec$|"
    r"codex$|"
    r"tokens used$|"
    r"[\d,]+$|"
    r"Filesystem\s+Size\s+Used|"
    r"/dev/disk\S+\s+|"
    r"\[exited(?:\s+with code -?\d+)?\]$|"
    r"(?:\[\d+/\d+\]\s*)?updating (?:github )?issue\b"
    r")",
    re.I,
)
_SHELL_CMD_RE = re.compile(r"^(?:/bin/)?(?:zsh|bash|sh)\s+-\w*c\s+(.+?)(?:\s+in\s+/\S+)?$")
_UNITY_IMPORTING_RE = re.compile(r"^Importing\s+'GUID:\s*[0-9a-f]+\s*-\s*Path:\s*([^']+)'?", re.I)
_UNITY_IMPORTED_RE = re.compile(r"^Imported\s+'GUID:\s*[0-9a-f]+'\s*->", re.I)
_MCP_EVENT_RE = re.compile(r"^mcp:\s*([^/\s]+/)?([^\s(]+)\s+(started|\(completed\))", re.I)


def summarize_activity_line(raw_line: str) -> str | None:
    """Turn a raw worker/agent output line into a short human-readable activity update, or None if it is spinner/noise."""
    line = raw_line.strip()
    if not line or "(Ctrl-C to cancel" in line:
        return None
    if line.lower().startswith("[stderr]"):
        line = line[8:].strip()
    if not line:
        return None
    line = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+\s*", "", line).strip()
    if not line or _ACTIVITY_NOISE_RE.search(line) or _UNITY_IMPORTED_RE.match(line):
        return None
    if mcp := _MCP_EVENT_RE.match(line):
        if "completed" in mcp[3].lower():
            return None
        return f"MCP: {mcp[2]}"[:160]
    if imp := _UNITY_IMPORTING_RE.match(line):
        return f"Importing {imp[1].strip()}"[:160]
    if cmd := _SHELL_CMD_RE.match(line):
        inner = cmd[1].strip().strip('"').strip("'").strip()
        if inner:
            return f"Running: {re.sub(r'\s+', ' ', inner)}"[:160]
    line = re.sub(r"^(?:==>\s*|[-•*]\s+)", "", line).strip()
    if not line or _ACTIVITY_NOISE_RE.search(line):
        return None
    return re.sub(r"\s+", " ", line)[:160]


class RunProgress:
    # These are the headers emitted by common.print_phase, not arbitrary model prose.
    LABELS = {
        "PLANNING": "Planning implementation", "SPEC SYNTHESIS": "Synthesizing feature spec",
        "RISK ASSESSMENT": "Assessing ambiguity & risk", "INTENT GATE": "Evaluating intent gate",
        "EXECUTION PLANNING": "Planning execution tasks", "PLAN GATE": "Validating plan gate",
        "REPLANNING": "Replanning remaining tasks", "PREVIEWING": "Generating interactive preview",
        "ANALYZING FEEDBACK": "Analyzing feedback impact", "REFINING": "Refining from user feedback",
        "REFACTORING": "Consolidating & refactoring", "REVERTING": "Reverting last iteration",
        "SCHEDULING": "Scheduling worker",
        "EXECUTION": "Starting execution", "GIT PREP": "Preparing git branch",
        "STATUS UPDATE": "Updating job status", "INVESTIGATION": "Investigating issue",
        "IMPLEMENTATION": "Implementing changes", "AI THINKING": "Working with AI",
        "BUILDING": "Building project", "TESTING": "Running tests",
        "VERIFICATION": "Verifying changes", "REVIEW": "Reviewing changes",
        "PULL REQUEST": "Creating pull request", "DELIVERY": "Delivering build",
        "EXPORTING": "Exporting job", "CLEANUP": "Cleaning up", "COMPLETE": "Complete",
        "DEBUG LOOP": "Debugging issue", "COVERAGE": "Measuring code coverage",
        "MEASURING CODE COVERAGE": "Measuring code coverage",
    }
    TASK_PHASES = {
        "INVESTIGATION", "IMPLEMENTATION", "AI THINKING", "BUILDING", "TESTING",
        "VERIFICATION", "COVERAGE", "MEASURING CODE COVERAGE",
        "SPEC SYNTHESIS", "RISK ASSESSMENT", "INTENT GATE", "EXECUTION PLANNING",
        "PLAN GATE", "REPLANNING", "PREVIEWING", "ANALYZING FEEDBACK",
        "REFINING", "REFACTORING", "REVERTING",
    }
    SUITE_PHASES = {"Investigating issue", "Implementing changes", "Working with AI", "Running tests", "Verifying changes"}
    # A later milestone signals that the work before it finished. These fractions estimate work within a numbered step.
    PLANNING_MILESTONES = {
        "Assessing ambiguity & risk": 0.2,
        "Evaluating intent gate": 0.4,
        "Planning execution tasks": 0.6,
        "Validating plan gate": 0.8,
    }
    TASK_MILESTONES = {
        "BUILDING": 0.5, "SKIPPING IOS BUILD": 0.5, "TESTING": 0.75, "SKIPPING TESTS": 0.75, "VERIFICATION": 0.9,
    }
    # A label from before any real work was announced: once a model call starts, say what is being waited on instead.
    IDLE_LABELS = {"", "Scheduling worker", "Starting execution", "Updating job status", "Preparing git branch", "Working with AI"}
    ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][AB0-2]|[78=>cM])")

    def __init__(self) -> None:
        self._pending = b""
        self.label = ""
        self.task = ""
        self.step: int | None = None
        self.total: int | None = None
        self.step_fraction = 0.0
        self._step_kind = ""
        self.models: list[str] = []
        self.active: list[str] = []
        self.is_coverage = False
        self.notice = ""
        self.machines: list[str] = []
        self.suites: list[str] = []
        self.suite = 0  # 1-based suite the builder is on; 0 until one is mentioned
        self.calls: list[dict] = []   # model calls in flight, oldest first: {"model", "since", "timeout"}
        self.failed: list[dict] = []  # models tried and given up on, with a short reason: {"model", "reason"}
        self.stall = 0                # minutes the running call has gone without output, as the worker reports it
        self.spec_stage = ""          # current adaptive planning pass (e.g. "Pass A · Spec synthesis", "Pass B · Execution planning")
        self.planning_depth = ""      # "sparse" | "partial" | "detailed"
        self.gate_state = ""          # current gate evaluation status (e.g. "Intent gate passed", "Plan gate · auto-passed")
        self.experience_version = ""  # current prototype/experience version (e.g. "v0.1", "v0.2")
        self.feedback_impact = ""     # classified user feedback impact (e.g. "behavior_change · updating spec & code")
        self.activity = ""            # latest meaningful agent/tool activity line (never clobbered by spinners)
        self._activity_ticks = 0      # distinct observed activity updates within the current phase

    def _start_step(self, step: int, total: int, description: str, kind: str) -> None:
        if (step, total, kind) != (self.step, self.total, self._step_kind):
            self.step_fraction = 0.0
            self._activity_ticks = 0
        self.step, self.total, self._step_kind = step, total, kind
        self.task = description.capitalize()
        self.label = self.task or "Running step"
        self.activity = ""

    def _advance_planning(self, label: str) -> None:
        if self._step_kind == "planning":
            self.step_fraction = max(self.step_fraction, self.PLANNING_MILESTONES.get(label, 0))

    def _advance_activity(self) -> None:
        """Advance within-step progress smoothly on real observed AI/tool activity before BUILDING (0.5)."""
        if self._step_kind != "task" or self.step_fraction >= 0.45:
            return
        self._activity_ticks += 1
        target = 0.45 * (1.0 - (0.85 ** self._activity_ticks))
        self.step_fraction = max(self.step_fraction, round(min(0.45, target), 3))

    def feed(self, chunk: bytes) -> None:
        # PTY reads can split a UTF-8 character or ANSI sequence. Decode complete lines only.
        lines = (self._pending + chunk).replace(b"\r", b"\n").split(b"\n")
        self._pending = lines.pop()[-16384:]
        for raw in lines:
            line = self.ANSI.sub(b"", raw).decode("utf-8", "replace").strip()
            header = re.fullmatch(r"=+\s*(.*?)\s*=+", line)
            if header:
                title = re.sub(r"^[^\w]+", "", header[1]).strip()
                if "COVERAGE" in title.upper():
                    self.is_coverage = True
                task = re.fullmatch(r"(SUB[- ]TASK|STEP)\s*(\d+)(?:-(\d+))?/(\d+)(?::\s*(.*))?", title, re.I)
                if task and 0 < int(task[2]) <= int(task[3] or task[2]) <= int(task[4]):
                    kind = "task" if task[1].upper().startswith("SUB") else "step"
                    self._start_step(int(task[2]), int(task[4]), (task[5] or "").strip(), kind)
                else:
                    phase, _, detail = title.partition(":")
                    phase = phase.strip().replace("_", " ").upper()
                    if self._step_kind == "task":
                        milestone = self.TASK_MILESTONES.get(phase, 0)
                        if milestone > self.step_fraction:
                            self.step_fraction = milestone
                            self._activity_ticks = 0
                    if phase in self.LABELS:
                        self.label = self.LABELS[phase]
                        self.activity = ""
                        self._advance_planning(self.label)
                        if phase == "SPEC SYNTHESIS":
                            self.spec_stage = "Pass A · Spec synthesis"
                        elif phase == "RISK ASSESSMENT":
                            self.spec_stage = "Pass A · Risk & ambiguity assessment"
                        elif phase == "INTENT GATE":
                            self.gate_state = detail.strip().capitalize() or "Evaluating intent gate"
                        elif phase == "EXECUTION PLANNING":
                            self.spec_stage = "Pass B · Execution planning"
                        elif phase == "PLAN GATE":
                            self.gate_state = detail.strip().capitalize() or "Validating plan gate"
                        elif phase == "REPLANNING":
                            self.spec_stage = "Mid-run replanning"
                        elif phase == "ANALYZING FEEDBACK":
                            self.feedback_impact = detail.strip().capitalize() or "Analyzing feedback impact"
                        elif phase == "REFINING":
                            self.spec_stage = "Experience loop · Refining"
                        elif phase == "REFACTORING":
                            self.spec_stage = "Experience loop · Refactoring"
                        elif phase == "REVERTING":
                            self.spec_stage = "Experience loop · Reverting"
                        if phase not in self.TASK_PHASES:
                            self.step = self.total = None
                            self._step_kind, self.step_fraction = "", 0.0
                            self.task = detail.strip().capitalize()
            else:
                in_sub_runner = self.is_coverage or self.label in (
                    "Running test suite with coverage",
                    "Running tests",
                    "Building project",
                )
                bracket = re.fullmatch(r"\[(\d+)/(\d+)\]\s*(.*)", line)
                if bracket and not in_sub_runner and 0 < int(bracket[1]) <= int(bracket[2]):
                    desc = bracket[3].strip()
                    kind = "planning" if bracket[1] == "1" and desc.startswith(("Planning ", "Fast-tracking a plan")) else "step"
                    self._start_step(int(bracket[1]), int(bracket[2]), desc, kind)

            notice = re.fullmatch(r"\[notice\]\s*(.+)", line)
            if notice:  # a finished run can say it only partly did its job
                self.notice = notice[1].strip()[:400]

            spec_line = re.fullmatch(r"\[spec\]\s*(\{.*\})", line)
            if spec_line:
                try:
                    spec_meta = json.loads(spec_line[1])
                    if isinstance(spec_meta, dict):
                        if spec_meta.get("stage"):
                            self.spec_stage = str(spec_meta["stage"]).strip()[:120]
                        if spec_meta.get("depth"):
                            self.planning_depth = str(spec_meta["depth"]).strip()[:40]
                        if spec_meta.get("label"):
                            self.label = str(spec_meta["label"]).strip()[:120]
                            self._advance_planning(self.label)
                except ValueError:
                    pass

            gate_line = re.fullmatch(r"\[gate\]\s*(\{.*\})", line)
            if gate_line:
                try:
                    gate_meta = json.loads(gate_line[1])
                    if isinstance(gate_meta, dict):
                        if gate_meta.get("state"):
                            self.gate_state = str(gate_meta["state"]).strip()[:120]
                            if self.gate_state.lower().startswith("plan gate"):
                                self._advance_planning(self.LABELS["PLAN GATE"])
                            elif self.gate_state.lower().startswith("intent gate"):
                                self._advance_planning(self.LABELS["INTENT GATE"])
                        if gate_meta.get("label"):
                            self.label = str(gate_meta["label"]).strip()[:120]
                except ValueError:
                    pass

            exp_line = re.fullmatch(r"\[experience\]\s*(\{.*\})", line)
            if exp_line:
                try:
                    exp_meta = json.loads(exp_line[1])
                    if isinstance(exp_meta, dict):
                        if exp_meta.get("version"):
                            ver = str(exp_meta["version"]).strip()
                            self.experience_version = ver if ver.startswith("v") else f"v{ver}"
                        if exp_meta.get("impact") or exp_meta.get("feedback_type"):
                            raw_imp = str(exp_meta.get("impact") or exp_meta.get("feedback_type") or "").strip()
                            self.feedback_impact = raw_imp[:120]
                        if exp_meta.get("stage"):
                            self.spec_stage = str(exp_meta["stage"]).strip()[:120]
                        if exp_meta.get("label"):
                            self.label = str(exp_meta["label"]).strip()[:120]
                except ValueError:
                    pass

            planned = re.fullmatch(r"\[suites\]\s*(\[.*\])", line)
            if planned:  # a coverage plan names the suites it will add
                try:
                    self.suites = [str(x) for x in json.loads(planned[1]) if str(x).strip()]
                except ValueError:
                    pass
                self.suite = 0
            elif self.suites and self.label in self.SUITE_PHASES:
                lowered = line.lower()
                for index in range(len(self.suites) - 1, -1, -1):  # newest mention wins, so later suites override earlier ones
                    name = self.suites[index].replace("\\", "/").rsplit("/", 1)[-1].lower()
                    if len(name) >= 3 and name in lowered:
                        self.suite = index + 1
                        break

            scheduled = re.search(r"\bScheduled \S+ group \S+ to (\S+) using", line)
            if scheduled and scheduled[1] not in self.machines:  # the worker machine(s) the job was handed to
                self.machines.append(scheduled[1])

            failure = re.match(r"⚠️\s+(\S+) (?:failed \((.*)|timed out)", line)
            if failure:
                self.failed = [f for f in self.failed if f["model"] != failure[1]] + [{"model": failure[1], "reason": self._reason(failure[2] or "", line)}]

            stalled = re.search(r"\[Stall Notice\] No output received for (\d+)m", line)
            if stalled:
                self.stall = int(stalled[1])

            start = re.search(r"Running LLM \(([^,)]+)(?:,\s*timeout=(\d+)s)?", line)
            if start:
                model = start[1].strip()
                self.active.append(model)
                self.calls.append({"model": model, "since": time.time(), "timeout": int(start[2]) if start[2] else 0})
                self.stall = 0
                if model not in self.models:
                    self.models.append(model)
                if self.label in self.IDLE_LABELS or self.label.startswith(("Waiting for ", "Reading the model")):
                    self.label = f"Waiting for {model}"
            elif re.search(r"\[output\] Received \d+ chars in [\d.]+s", line):
                if self.active:
                    self.active.pop(0)
                    if self.calls:
                        self.calls.pop(0)
                self.stall = 0
                self.activity = ""
                if self._step_kind == "task" and self.step_fraction < 0.45:
                    self.step_fraction = 0.45
                if self.label.startswith("Waiting for "):
                    self.label = "Reading the model's answer"
            elif outcome := MODEL_QUOTA.match(line) or failure:
                model = outcome[1]
                if model in self.active:
                    self.active.remove(model)
                    call = next((c for c in self.calls if c["model"] == model), None)
                    if call:
                        self.calls.remove(call)
                self.stall = 0
            elif "Attempting fallback..." in line:
                for model in list(self.active):
                    if model in line or line.startswith(f"⚠️  {model} timed out.") or line.startswith(f"⚠️  {model} failed ("):
                        self.active.remove(model)
                        call = next((c for c in self.calls if c["model"] == model), None)
                        if call:
                            self.calls.remove(call)
                        break
            else:
                using_match = re.search(r"(?:using|Consulting)\s+([a-zA-Z0-9_\-\./]+)\.\.\.", line)
                if using_match:
                    model = using_match[1].strip()
                    if model and model not in self.models and ("/" in model or "." in model or any(k in model for k in ("gpt", "claude", "gemini", "qwen", "deepseek", "nemotron", "codex", "llama"))):
                        self.models.append(model)
                if not header and not notice and not spec_line and not gate_line and not exp_line and not planned:
                    summary = summarize_activity_line(line)
                    if summary:
                        if summary != self.activity:
                            self.activity = summary
                            self._advance_activity()

    @staticmethod
    def _reason(detail: str, line: str) -> str:
        """Why a model was given up on, in a few words, from the worker's failure line."""
        if "timed out" in line.split("(")[0]:
            return "timed out"
        message = re.search(r'"message":"([^"]+)"', detail)
        if message:
            return message[1].split(". ")[0].rstrip(".")[:140]
        code = re.search(r"code (\d+)", detail)
        if code:
            return "couldn't start (is it installed?)" if code[1] == "127" else f"exited with code {code[1]}"
        return " ".join(detail.rstrip(") ").split())[:140] or "failed"

    def snapshot(self, running: bool = True) -> dict:
        active = self.active if running else []
        now = time.time()
        calls = [{"model": c["model"], "age": int(now - c["since"]), "timeout": c["timeout"]} for c in self.calls] if running else []
        return {"calls": calls, "failed": list(self.failed), "stall": self.stall if running else 0, **self._base(active)}

    def _base(self, active: list[str]) -> dict:
        out = {"label": self.label, "task": self.task, "step": self.step, "total": self.total, "step_fraction": self.step_fraction,
               "notice": self.notice, "machines": list(self.machines), "suites": list(self.suites), "suite": self.suite,
               "models": list(self.models), "active_models": list(dict.fromkeys(active)), "agents": len(active)}
        if self.spec_stage:
            out["spec_stage"] = self.spec_stage
        if self.planning_depth:
            out["planning_depth"] = self.planning_depth
        if self.gate_state:
            out["gate_state"] = self.gate_state
        if self.experience_version:
            out["experience_version"] = self.experience_version
        if self.feedback_impact:
            out["feedback_impact"] = self.feedback_impact
        if self.activity:
            out["activity"] = self.activity
        return out
