"""What a finished run found, in a line or two, read from its output.

The run page used to say only "Completed (exit 0)". `summarize` turns the output into a headline and a few details, with a reader
for the runs whose output has a shape (fleet sync and checks, tests, coverage, screenshots, UX review) and a general one for the rest:
the lines that say how it went. Nothing here runs anything; it only reads text, and returns None when there is nothing to say.
"""
from __future__ import annotations

import re
from typing import Any, Callable

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
SPINNER = re.compile("[⠀-⣿]")
EXIT_MARK = re.compile(r"^\[exited(?: with code \d+)?\]$")
RULE = re.compile(r"^[=\-─━_*#\s]{4,}$")
MARKS = ("✅", "❌", "⚠️", "⚠", "🟡", "✔", "✘", "🚀")
OK_MARKS, BAD_MARKS, WARN_MARKS = ("✅", "✔"), ("❌", "✘"), ("⚠️", "⚠", "🟡")
MAX_LINE = 200


def clean_lines(text: str) -> list[str]:
    text = SPINNER.sub("", ANSI.sub("", text)).replace("\r", "\n")
    return [line.strip() for line in text.splitlines() if line.strip() and not EXIT_MARK.match(line.strip()) and not RULE.match(line.strip())]


def plain(line: str) -> str:
    """The line without its bullet, leading status mark and 'ERROR:' label."""
    line = line.strip()
    for _ in range(3):
        line = re.sub(r"^(?:[-•*]\s+|ERROR:\s*|Error:\s*)", "", line)
        for mark in MARKS:
            if line.startswith(mark):
                line = line[len(mark):].strip()
                break
    return line.strip()[:MAX_LINE]


def _tone(line: str) -> str:
    if line.startswith(BAD_MARKS):
        return "bad"
    if line.startswith(WARN_MARKS):
        return "warn"
    return "ok"


def _result(headline: str, details: list[str] | None = None, tone: str = "ok") -> dict[str, Any]:
    return {"headline": headline[:MAX_LINE], "details": [d[:MAX_LINE] for d in (details or [])][:8], "tone": tone}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---- fleet: "--- Machine: name ---" blocks (sync, model check)

BLOCK = re.compile(r"^---\s*Machine:\s*(.+?)\s*---$")


def machine_blocks(lines: list[str]) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current = None
    for line in lines:
        found = BLOCK.match(line)
        if found:
            current = found.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def sync_fleet(lines: list[str], code: int | None) -> dict[str, Any] | None:
    blocks = machine_blocks(lines)
    if not blocks:
        return None
    summary = {m.group(1).strip(): "OK" in m.group(2) and "FAILED" not in m.group(2)
               for m in (re.match(r"^(\S+)\s*:\s*(?:✅|❌)\s*(.+)$", l) for l in lines) if m}
    state = next((re.search(r"branch='([^']*)', hash='([^']*)', dirty=(\w+)", l) for l in lines if l.startswith("Local state:")), None)
    details, failed, synced, local = [], [], 0, 0
    for name, body in blocks.items():
        is_local = name.endswith("(Local)") or any("local machine" in l and "Skipping" in l for l in body)
        label = re.sub(r"\s*\(Local\)$", "", name)
        ok = summary.get(label, not any("ERROR" in l for l in body))
        if is_local:
            local += 1
            details.append(f"{label}: this computer, nothing to sync")
        elif ok:
            synced += 1
            details.append(f"{label}: code synced")
        else:
            failed.append(label)
            reason = next((plain(l) for l in body if "ERROR" in l), "")
            details.append(f"{label}: failed" + (f", {reason}" if reason else ""))
    if state:
        branch, rev, dirty = state.groups()
        details.insert(0, f"From {branch} at {rev[:8]}" + (", with uncommitted changes that are not synced" if dirty == "True" else ""))
    remote = synced + len(failed)
    if failed:
        return _result(f"{_plural(len(failed), 'machine')} failed to sync" + (f", {_plural(synced, 'machine')} synced" if synced else ""), details, "bad")
    if not remote:
        return _result("No other machines to sync", details + ["Add a worker under Configuration > Machines to spread work across computers."])
    return _result(f"Synced {_plural(remote, 'machine')}", details)


def fleet_llm_check(lines: list[str], code: int | None) -> dict[str, Any] | None:
    blocks = machine_blocks(lines)
    if not blocks:
        return None
    details, bad, fine = [], [], 0
    for name, body in blocks.items():
        failed = next((l for l in body if "FAILED" in l and "(" in l), "")
        error = next((l for l in body if "ERROR" in l), "")
        passed = next((l for l in body if "PASSED" in l), "")
        limited = next((l for l in body if "LIMITED" in l), "")
        if failed or error:
            bad.append(name)
            details.append(f"{name}: " + (plain(error) if error else re.sub(r"^FAILED\s*", "", plain(failed)) + " failed"))
        else:
            fine += 1
            count = re.search(r"\((\d+)\)", passed)
            details.append(f"{name}: " + (f"{count.group(1)} models responded" if count else "responded") + (", some limited" if limited else ""))
    if bad:
        return _result(f"Models failed on {_plural(len(bad), 'machine')}: {', '.join(bad)}", details, "bad")
    return _result(f"Models respond on {_plural(fine, 'machine')}", details)


def worker_check(lines: list[str], code: int | None) -> dict[str, Any] | None:
    summary = next((l for l in reversed(lines) if re.search(r"machines? ready|No machines are enabled", l)), "")
    if not summary:
        return None
    details, name = [], ""
    for line in lines:
        header = re.match(r"^\[([^\]]+)\]", line)
        if header:
            name = header.group(1)
        elif name and re.match(r"^(worker|package|xcodebuild):\s*(NOT READY|MISSING)", line):
            details.append(f"{name}: {line.split(':', 1)[0]} {line.split(':', 1)[1].strip().lower()}")
        elif name and line.startswith("details:"):
            details.append(f"{name}: {line[8:].strip()}")
    return _result(plain(summary), details, _tone(summary))


# ---- tests and coverage

def tests(lines: list[str], code: int | None) -> dict[str, Any] | None:
    text = "\n".join(lines)
    passed = failed = None
    found = re.search(r"Ran (\d+) tests? in", text)  # unittest
    if found:
        total = int(found.group(1))
        bad = re.search(r"FAILED \(([^)]*)\)", text)
        failed = sum(int(n) for n in re.findall(r"(\d+)", bad.group(1))) if bad else 0
        passed = max(0, total - failed)
    if passed is None:  # pytest: "=== 1 failed, 3 passed, 2 skipped in 0.12s ==="
        line = next((l for l in reversed(lines) if re.search(r"\bin [\d.]+s\b", l) and re.search(r"\d+ (?:passed|failed|errors?)", l)), "")
        if line:
            count = lambda word: sum(int(n) for n in re.findall(rf"(\d+) {word}", line))
            failed, passed = count("failed") + count("errors?"), count("passed")
    if passed is None:
        found = re.search(r"Executed (\d+) tests?, with (\d+) failures?", text)  # XCTest
        if found:
            failed = int(found.group(2))
            passed = int(found.group(1)) - failed
    if passed is None:
        found = re.search(r"Test run with (\d+) tests? (passed|failed)", text)  # Swift Testing
        if found:
            passed, failed = (int(found.group(1)), 0) if found.group(2) == "passed" else (0, int(found.group(1)))
    if passed is None:
        return None
    total = passed + failed
    if failed:
        names = [re.sub(r"^(?:FAIL|ERROR)[:\s]+", "", plain(l)) for l in lines if re.match(r"^(?:FAIL|ERROR|✘|✖|❌)[:\s]", l)][:5]
        return _result(f"{failed} of {_plural(total, 'test')} failed", names, "bad")
    return _result(f"{_plural(total, 'test')} passed" if total else "No tests ran", [], "ok" if total else "warn")


def coverage(lines: list[str], code: int | None) -> dict[str, Any] | None:
    for line in reversed(lines):
        found = re.search(r"coverage[^%\d]*?(\d+(?:\.\d+)?)\s*%", line, re.I) or re.search(r"(\d+(?:\.\d+)?)\s*%[^\n]*coverage", line, re.I)
        if found and not re.search(r"fail|error", line, re.I):
            return _result(f"Coverage is {found.group(1)}%", [plain(line)] if plain(line).lower() != f"coverage is {found.group(1)}%" else [], "ok")
    return None


def visual_check(lines: list[str], code: int | None) -> dict[str, Any] | None:
    found = next((re.search(r"Screenshots:\s*(\d+)\s*screenshot", l) for l in reversed(lines) if "Screenshots:" in l), None)
    if not found:
        return None
    n = int(found.group(1))
    return _result(f"Captured {_plural(n, 'screenshot')}", [], "ok" if n else "warn")


def ux_pass(lines: list[str], code: int | None) -> dict[str, Any] | None:
    for line in reversed(lines):
        found = re.match(r"^(\d+) findings? \((\d+) major or worse\)", line)
        if found:
            n, major = int(found.group(1)), int(found.group(2))
            return _result(f"{_plural(n, 'finding')}" + (f", {major} major or worse" if major else ""), [], "bad" if major else "warn" if n else "ok")
        if line.startswith("Nothing to review"):
            return _result("Nothing to review yet", ["There are no interface files or screens in this project."], "warn")
    return None


def plan_epic(lines: list[str], code: int | None, job_title: str = "") -> dict[str, Any] | None:
    """Planning an epic makes a job (a plan, waiting for approval) and a GitHub issue; nothing is built or branched."""
    if code or not any("Skipping dispatch" in l or "Created issue" in l for l in lines):
        return None
    issue = next((m.group(1) for m in (re.search(r"Created issue:\s*#(\d+)", l) for l in lines) if m), "")
    details = [f"Job: {job_title}"] if job_title else []
    details += ["Planned only: no code was written and no branch was created, so nothing has changed in your project.",
                "Open the job to review the plan, then approve it to start building. It is also under Jobs on Home, and on the epic's row in Product."]
    if issue:
        details.append(f"GitHub issue #{issue} was created for it.")
    return _result("Plan ready to review", details)


READERS: dict[str, Callable[[list[str], int | None], dict[str, Any] | None]] = {
    "sync_fleet": sync_fleet, "fleet_llm_check": fleet_llm_check, "worker_check": worker_check,
    "test": tests, "test_suite": tests, "test_plan": tests, "coverage": coverage, "visual_check": visual_check, "ux_pass": ux_pass,
}


# ---- everything else: the lines that say how it went

def general(lines: list[str], code: int | None) -> dict[str, Any] | None:
    if not lines:
        return None
    marked = [l for l in lines if l.startswith(MARKS)]
    if code:  # failed: the most specific error line leads
        errors = [l for l in marked if _tone(l) == "bad"] or [l for l in lines if re.match(r"^(?:Error|error|fatal|FAILED|!!!)", l)]
        head = plain(errors[-1]) if errors else plain(lines[-1])
        return _result(head, [plain(l) for l in marked if plain(l) != head][-3:], "bad")
    if marked:
        last = marked[-1]
        return _result(plain(last), [plain(l) for l in marked[-4:-1]], _tone(last))
    return _result(plain(lines[-1]), [plain(l) for l in lines[-4:-1]])


def summarize(action: str, text: str, exit_code: int | None, job_title: str = "") -> dict[str, Any] | None:
    """{headline, details, tone} for a finished run, or None when its output says nothing. `job_title` names the job a run made."""
    lines = clean_lines(text)
    reader = READERS.get(action)
    try:
        found = plan_epic(lines, exit_code, job_title) if action == "plan_epic" else reader(lines, exit_code) if reader else None
        if found and exit_code and found["tone"] == "ok":
            found["tone"] = "bad"  # a reader found a good-looking line, but the run still failed
        return found or general(lines, exit_code)
    except Exception:  # a reader is a nicety: never let it hide the run's own status
        return general(lines, exit_code)
