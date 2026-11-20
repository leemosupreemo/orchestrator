"""The checks before the hosted web app is republished (`scripts/republish.py`).

Republishing is `firebase deploy --only hosting`: it uploads `orchestrator/web/static` exactly as it is on disk. Nothing used to
check that first, so a deploy went out with ten tests already failing and, since the working tree is what ships, could carry a
parallel session's half-finished edits. Two gates:

- the working tree's `orchestrator/web/static` must be committed (what ships is what the branch says);
- the whole suite runs, and a failure that is not in `tests/known_failures.txt` blocks the deploy. The file lists failures that
  already exist (with why), so debt doesn't block every deploy forever but a new failure always does. A listed test that
  passes again is reported so its line can be removed.

The pieces are pure or take their process runner as an argument, so the tests need no Firebase and no real suite.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

STATIC_DIR = "orchestrator/web/static"
BASELINE_FILE = "tests/known_failures.txt"
DEPLOY_COMMAND = ["firebase", "deploy", "--only", "hosting", "--non-interactive"]
# Runs the suite the way `python3 -m unittest discover -s tests` does (or the named modules) and writes what happened as JSON, so
# nothing has to be parsed out of unittest's text: tests that run unittest themselves print their own "Ran N tests" lines.
RUNNER = """
import json, sys, unittest
out, names = sys.argv[1], sys.argv[2:]
loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(names) if names else loader.discover("tests")
failing = []
class Result(unittest.TextTestResult):
    def _note(self, test):
        if test.id() not in failing:
            failing.append(test.id())
    def addFailure(self, test, err):
        super().addFailure(test, err); self._note(test)
    def addError(self, test, err):
        super().addError(test, err); self._note(test)
    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._note(test)
result = unittest.TextTestRunner(resultclass=Result, verbosity=0).run(suite)
json.dump({"ran": result.testsRun, "failing": failing}, open(out, "w"))
sys.exit(0 if result.wasSuccessful() else 1)
"""


def normalize_id(test_id: str) -> str:
    """The same test, however the suite was started: `tests.test_x.Case.test` and `test_x.Case.test` are one."""
    return test_id.strip().removeprefix("tests.")


def read_baseline(path: Path) -> dict[str, str]:
    """{test id: why it is allowed to fail} from the known-failures file: one `id  # reason` per line, `#` lines are notes."""
    known: dict[str, str] = {}
    if not path.is_file():
        return known
    for line in path.read_text(encoding="utf-8").splitlines():
        body, _, reason = line.partition("#")
        if body.strip() and not line.lstrip().startswith("#"):
            known[normalize_id(body.split()[0])] = reason.strip()
    return known


@dataclass
class Verdict:
    new: list[str] = field(default_factory=list)       # failing and not in the baseline: blocks the deploy
    known: list[str] = field(default_factory=list)     # failing and in the baseline: allowed
    fixed: list[str] = field(default_factory=list)     # in the baseline but passing now: its line can go
    problem: str = ""                                  # the suite didn't run properly: blocks the deploy

    @property
    def ok(self) -> bool:
        return not self.new and not self.problem


def judge(failing: list[str], baseline: dict[str, str], *, returncode: int, ran: int) -> Verdict:
    verdict = Verdict(new=[t for t in failing if t not in baseline], known=[t for t in failing if t in baseline])
    verdict.fixed = [t for t in baseline if t not in failing] if ran else []
    if not ran:
        verdict.problem = "No tests ran, so there is nothing to say the web app works."
    elif returncode != 0 and not failing:
        verdict.problem = "The test run failed without naming a failing test (it may have crashed), so it can't be trusted."
    return verdict


def dirty_static(root: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[str]:
    """Uncommitted changes under the shipped folder: a deploy would upload them though no branch has them."""
    res = run(["git", "status", "--porcelain", "--", STATIC_DIR], cwd=str(root), capture_output=True, text=True, check=False)
    return [line[3:] for line in res.stdout.splitlines() if line.strip()]


def run_suite(root: Path, *, only: list[str] | None = None, log: Path | None = None,
              run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> tuple[int, dict]:
    """Run the suite (all of it, or just the named modules) and say what happened: (exit code, {"ran": n, "failing": [ids]}).
    The result is empty when the runner died before finishing; the output goes to `log` either way."""
    with tempfile.TemporaryDirectory() as scratch:
        result_file = Path(scratch) / "result.json"
        res = run([sys.executable, "-c", RUNNER, str(result_file), *(only or [])], cwd=str(root), capture_output=True, text=True, check=False)
        if log:
            log.write_text(f"{res.stdout or ''}{res.stderr or ''}", encoding="utf-8")
        try:
            data = json.loads(result_file.read_text())
        except (OSError, ValueError):
            return res.returncode, {"ran": 0, "failing": []}
    return res.returncode, {"ran": int(data.get("ran") or 0), "failing": [normalize_id(t) for t in data.get("failing") or []]}


def hosting_url(root: Path) -> str:
    try:
        project = json.loads((root / ".firebaserc").read_text())["projects"]["default"]
    except (OSError, ValueError, KeyError):
        return ""
    return f"https://{project}.web.app"


def served_count(url: str, symbol: str, fetch: Callable[[str], str] | None = None) -> int:
    """How many times `symbol` appears in the hosted app.js: the check that the new version is what visitors get."""
    def get(address: str) -> str:
        with urllib.request.urlopen(address, timeout=20) as response:  # noqa: S310 - the project's own hosting URL
            return response.read().decode("utf-8", "replace")
    return (fetch or get)(f"{url.rstrip('/')}/app.js").count(symbol)
