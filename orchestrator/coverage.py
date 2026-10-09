"""Code coverage for projects that aren't built with Xcode, measured by each language's own tool.

Each tool counts something slightly different (coverage.py and Go count statements, llvm-cov and Istanbul count
lines), so the result records which tool measured it and what it counted. When a project's tool isn't installed or
isn't one we know, the result says so and how to fix it: a number is only ever a measurement, never a guess.
Xcode projects are measured by the console's xcodebuild path (scripts/dev_console.py).
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

Run = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


class CoverageUnavailable(Exception):
    """This project's coverage can't be measured here; the message says why and what to do."""


@dataclass
class Measurement:
    pct: float
    tool: str
    metric: str  # what was counted: "lines" or "statements"
    total_lines: int | None = None
    covered_lines: int | None = None


def count_source_lines(root: Path) -> int:
    """Fast count of source code lines across common programming languages."""
    if not root.is_dir():
        return 0
    exts = {
        ".swift", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".cpp",
        ".h", ".hpp", ".m", ".mm", ".kt", ".java", ".rb", ".sh",
    }
    ignored = {
        ".git", ".build", "node_modules", "Pods", "DerivedData", ".venv", "venv",
        "__pycache__", ".orchestrator", "dist", "build", "coverage", ".pytest_cache",
    }
    total = 0
    try:
        import os
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".")]
            for f in files:
                if os.path.splitext(f)[1].lower() in exts:
                    try:
                        with open(os.path.join(r, f), "rb") as fp:
                            total += sum(1 for _ in fp)
                    except OSError:
                        pass
        return total
    except Exception:
        return 0


def stream(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    """Runs a command with its output shown as it happens (it lands in the run's log) and kept for parsing."""
    print(f"\033[90m$ {shlex.join(argv)}\033[0m", flush=True)
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in proc.stdout or []:
        sys.stdout.write(line)
        lines.append(line)
    proc.wait()
    return subprocess.CompletedProcess(argv, proc.returncode, "".join(lines), "")


def _python_module_works(module: str, run: Run, root: Path) -> bool:
    try:
        return run([sys.executable, "-m", module, "--version"], root).returncode == 0
    except OSError:
        return False


def _package_json(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def python(root: Path, test_command: str, run: Run, out: Path) -> Measurement:
    if not _python_module_works("coverage", run, root):
        print("\n=== STEP 1/5: SETTING UP COVERAGE.PY ===", flush=True)
        print("coverage.py is not installed yet. Automatically setting up coverage.py...\n", flush=True)
        install_res = run([sys.executable, "-m", "pip", "install", "coverage"], root)
        if install_res.returncode != 0 or not _python_module_works("coverage", run, root):
            raise CoverageUnavailable(
                f"Attempted to auto-install coverage.py but installation failed (exit code {install_res.returncode}). "
                "Install it manually with `pip install coverage`, then measure again."
            )
        print("\n✅ coverage.py installed successfully.\n", flush=True)
    else:
        print("\n=== STEP 1/5: CHECKING COVERAGE ENVIRONMENT ===", flush=True)
        print("Verified coverage.py is installed and ready.\n", flush=True)

    print("=== STEP 2/5: PREPARING TEST RUNNER ===", flush=True)
    words = shlex.split(test_command or "python3 -m unittest discover")
    if words[:1] == ["pytest"]:
        target = ["-m", "pytest", *words[1:]]
    elif len(words) >= 3 and words[1] == "-m":  # python3 -m unittest discover tests
        target = ["-m", *words[2:]]
    else:
        raise CoverageUnavailable(f"Couldn't see how to measure coverage for the test command `{test_command}`. "
                                  "Use pytest or `python3 -m unittest`, or run coverage.py yourself.")

    print(f"Configured test runner target: {' '.join(target)}\n", flush=True)

    print("=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===", flush=True)
    print("Executing tests under coverage instrumentation...\n", flush=True)
    if run([sys.executable, "-m", "coverage", "run", *target], root).returncode != 0:
        raise CoverageUnavailable("The tests failed, so coverage wasn't recorded. Fix the failing tests and measure again.")

    print("\n=== STEP 4/5: GENERATING COVERAGE REPORT ===", flush=True)
    report = out / "coverage.json"
    run([sys.executable, "-m", "coverage", "json", "-o", str(report)], root)

    print("\n=== STEP 5/5: ANALYZING COVERAGE METRICS ===", flush=True)
    totals = json.loads(report.read_text(encoding="utf-8"))["totals"]
    total_stmts = int(totals["num_statements"]) if "num_statements" in totals else None
    covered = int(totals["covered_lines"]) if "covered_lines" in totals else (int(totals["covered_statements"]) if "covered_statements" in totals else None)
    return Measurement(round(float(totals["percent_covered"]), 1), "coverage.py", "statements", total_lines=total_stmts, covered_lines=covered)


def go(root: Path, run: Run, out: Path) -> Measurement:
    print("\n=== STEP 1/5: CHECKING GO TEST ENVIRONMENT ===", flush=True)
    profile = out / "cover.out"
    print("\n=== STEP 2/5: PREPARING COVERAGE PROFILE ===", flush=True)
    print("\n=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===", flush=True)
    if run(["go", "test", f"-coverprofile={profile}", "./..."], root).returncode != 0:
        raise CoverageUnavailable("The tests failed, so coverage wasn't recorded. Fix the failing tests and measure again.")
    print("\n=== STEP 4/5: GENERATING COVERAGE REPORT ===", flush=True)
    summary = run(["go", "tool", "cover", f"-func={profile}"], root).stdout
    m = re.search(r"^total:\s+\(statements\)\s+([\d.]+)%", summary, re.M)
    if not m:
        raise CoverageUnavailable("Go didn't report a coverage total.")
    print("\n=== STEP 5/5: ANALYZING COVERAGE METRICS ===", flush=True)
    return Measurement(float(m.group(1)), "go test -cover", "statements")


def rust(root: Path, run: Run, out: Path) -> Measurement:
    print("\n=== STEP 1/5: CHECKING CARGO-LLVM-COV TOOL ===", flush=True)
    try:
        installed = run(["cargo", "llvm-cov", "--version"], root).returncode == 0
    except OSError:
        installed = False
    if not installed:
        raise CoverageUnavailable("Rust coverage needs cargo-llvm-cov. Install it with `cargo install cargo-llvm-cov`, then measure again.")
    print("\n=== STEP 2/5: PREPARING RUST COVERAGE ENVIRONMENT ===", flush=True)
    report = out / "llvm-cov.json"
    print("\n=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===", flush=True)
    if run(["cargo", "llvm-cov", "--json", "--summary-only", "--output-path", str(report)], root).returncode != 0:
        raise CoverageUnavailable("The tests failed, so coverage wasn't recorded. Fix the failing tests and measure again.")
    print("\n=== STEP 4/5: GENERATING COVERAGE REPORT ===", flush=True)
    totals = json.loads(report.read_text(encoding="utf-8"))["data"][0]["totals"]
    print("\n=== STEP 5/5: ANALYZING COVERAGE METRICS ===", flush=True)
    total_lines = int(totals["lines"]["count"]) if "lines" in totals and "count" in totals["lines"] else None
    covered_lines = int(totals["lines"]["covered"]) if "lines" in totals and "covered" in totals["lines"] else None
    return Measurement(round(float(totals["lines"]["percent"]), 1), "cargo llvm-cov", "lines", total_lines=total_lines, covered_lines=covered_lines)


def swift_package(root: Path, run: Run, out: Path) -> Measurement:
    print("\n=== STEP 1/5: CHECKING SWIFT PACKAGE CONFIGURATION ===", flush=True)
    print("\n=== STEP 2/5: PREPARING TEST RUNNER ===", flush=True)
    print("\n=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===", flush=True)
    if run(["swift", "test", "--enable-code-coverage"], root).returncode != 0:
        raise CoverageUnavailable("The tests failed, so coverage wasn't recorded. Fix the failing tests and measure again.")
    print("\n=== STEP 4/5: EXPORTING COVERAGE REPORT ===", flush=True)
    path = run(["swift", "test", "--show-codecov-path"], root).stdout.strip().splitlines()[-1]
    totals = json.loads(Path(path).read_text(encoding="utf-8"))["data"][0]["totals"]
    print("\n=== STEP 5/5: ANALYZING COVERAGE METRICS ===", flush=True)
    total_lines = int(totals["lines"]["count"]) if "lines" in totals and "count" in totals["lines"] else None
    covered_lines = int(totals["lines"]["covered"]) if "lines" in totals and "covered" in totals["lines"] else None
    return Measurement(round(float(totals["lines"]["percent"]), 1), "swift test (llvm-cov)", "lines", total_lines=total_lines, covered_lines=covered_lines)


def javascript(root: Path, run: Run, out: Path) -> Measurement:
    print("\n=== STEP 1/5: CHECKING NODE TEST RUNNER AND COVERAGE TOOL ===", flush=True)
    pkg = _package_json(root)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    script = str(pkg.get("scripts", {}).get("test", ""))
    if "vitest" in deps or "vitest" in script:
        if not any(d in deps for d in ("@vitest/coverage-v8", "@vitest/coverage-istanbul")):
            raise CoverageUnavailable("Vitest needs a coverage provider. Add one with `npm i -D @vitest/coverage-v8`, then measure again.")
        argv = ["npx", "vitest", "run", "--coverage", "--coverage.reporter=json-summary", f"--coverage.reportsDirectory={out}"]
        tool = "Vitest"
    elif "jest" in deps or "jest" in script:
        argv = ["npx", "jest", "--coverage", "--coverageReporters=json-summary", f"--coverageDirectory={out}"]
        tool = "Jest"
    else:
        raise CoverageUnavailable("Coverage is measured for Jest and Vitest. This project's test tool isn't one of them, "
                                  "so its coverage can't be measured here yet.")
    print("\n=== STEP 2/5: PREPARING COVERAGE OUTPUT DIRECTORY ===", flush=True)
    print("\n=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===", flush=True)
    if run(argv, root).returncode != 0:
        raise CoverageUnavailable("The tests failed, so coverage wasn't recorded. Fix the failing tests and measure again.")
    print("\n=== STEP 4/5: GENERATING COVERAGE REPORT ===", flush=True)
    total = json.loads((out / "coverage-summary.json").read_text(encoding="utf-8"))["total"]
    print("\n=== STEP 5/5: ANALYZING COVERAGE METRICS ===", flush=True)
    total_lines = int(total["lines"]["total"]) if "lines" in total and "total" in total["lines"] else None
    covered_lines = int(total["lines"]["covered"]) if "lines" in total and "covered" in total["lines"] else None
    return Measurement(round(float(total["lines"]["pct"]), 1), tool, "lines", total_lines=total_lines, covered_lines=covered_lines)


def measure(root: Path, test_command: str | None, run: Run = stream) -> Measurement:
    """Coverage for a project that isn't built with Xcode, by the language's own tool."""
    with tempfile.TemporaryDirectory(prefix="orchestrator-coverage-") as tmp:
        out = Path(tmp)
        command = test_command or ""
        if (root / "go.mod").exists():
            return go(root, run, out)
        if (root / "Cargo.toml").exists():
            return rust(root, run, out)
        if (root / "Package.swift").exists():
            return swift_package(root, run, out)
        if (root / "package.json").exists():
            return javascript(root, run, out)
        if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "requirements.txt")) or "pytest" in command or "unittest" in command:
            return python(root, command, run, out)
        if any((root / f).exists() for f in ("build.gradle", "build.gradle.kts", "pom.xml")):
            raise CoverageUnavailable("Java and Kotlin coverage comes from JaCoCo, which has to be set up in the build. "
                                      "It can't be measured here yet.")
        raise CoverageUnavailable("Couldn't tell which language this project uses, so coverage can't be measured here.")
