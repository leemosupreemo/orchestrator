"""A run's output as steps: the run page's Timeline."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator.web.run_timeline import RunTimeline, from_log, plain_title  # noqa: E402
from tests.test_web_ui import ServerTestCase, ui  # noqa: E402

H = lambda text: f"\x1b[1;96m===================== {text} =====================\x1b[0m"
LOG = "\n".join([
    "\x1b[90mContext: /Users/me/Projects/Snake\x1b[0m",
    H("🏗️ EXECUTION"),
    H("🌿 GIT PREP"),
    "[1/4] Preparing git branch: ai/job-3-online...",
    "      - Creating new branch from main: ai/job-3-online",
    H("📊 STATUS UPDATE"),
    "[2/4] Updating job 3 status to 'executing'...",
    H("⚙️ SUB-TASK 1/2: WRITE THE GAME RULES"),
    "      - Running LLM (claude-sonnet-4-6, timeout=1200s)...",
    "        [output] Received 5120 chars in 31.4s",
    H("⚙️ SUB-TASK 2/2: BUILD THE ONLINE SCREEN"),
    "      - Running LLM (gpt-5.4, timeout=1200s)...",
    "⚠️  gpt-5.4 timed out. Attempting fallback...",
    "      - Running LLM (claude-sonnet-4-6, timeout=1200s)...",
    "        [output] Received 8800 chars in 48.0s",
    H("🔨 BUILDING"),
    "[1/3] transforming...",
    "[2/3] rendering chunks...",
    "✓ built in 2.31s",
    H("🧪 TESTING"),
    " ❌ versus.test.ts > two phones can finish a match",
    "Error: expected \"YOU WIN\" to be visible",
    "[4/4] Implementation successful. Opening/updating Pull Request...",
    "      - PR #12 opened",
])


class StepsTests(unittest.TestCase):
    def steps(self, text, code=0):
        return from_log(text + f"\n[exited{'' if code == 0 else f' with code {code}'}]").view(running=False)["steps"]

    def test_the_output_becomes_a_few_plain_steps(self):
        steps = self.steps(LOG)
        self.assertEqual([s["title"] for s in steps], [
            "Starting execution", "Preparing git branch: ai/job-3-online", "Updating job 3 status to 'executing'",
            "Write the game rules", "Build the online screen", "Building project", "Running tests",
            "Implementation successful. Opening/updating Pull Request"])
        self.assertEqual(steps[3]["of"], "1 of 2")  # a task of the plan says which
        self.assertEqual([s["kind"] for s in steps[1:8]], ["git", "run", "code", "code", "build", "test", "review"])

    def test_a_header_and_the_line_that_says_it_exactly_are_one_step(self):
        titles = [s["title"] for s in self.steps(LOG)]
        self.assertNotIn("Preparing git branch", titles)
        self.assertNotIn("Updating job status", titles)

    def test_a_build_tools_own_counters_are_not_steps_but_the_workers_are(self):
        titles = [s["title"] for s in self.steps(LOG)]
        self.assertNotIn("Transforming", titles)
        self.assertIn("Implementation successful. Opening/updating Pull Request", titles)  # [4/4] inside the test run

    def test_model_calls_are_listed_in_the_step_they_belong_to(self):
        build = self.steps(LOG)[4]
        self.assertEqual([(e["model"], e["status"], e["seconds"]) for e in build["events"]],
                         [("gpt-5.4", "failed", None), ("claude-sonnet-4-6", "done", 48)])
        self.assertEqual(build["events"][0]["reason"], "timed out")

    def test_error_lines_are_counted_and_a_failed_run_fails_its_last_step(self):
        steps = self.steps(LOG, code=1)
        tests = next(s for s in steps if s["title"] == "Running tests")
        self.assertEqual(tests["errors"], 2)
        self.assertEqual(steps[-1]["status"], "failed")
        self.assertTrue(all(s["status"] == "done" for s in self.steps(LOG)))

    def test_each_step_keeps_its_own_output(self):
        timeline = from_log(LOG)
        tests = next(s for s in timeline.view(False)["steps"] if s["title"] == "Running tests")
        self.assertEqual(timeline.lines(tests["id"]), [" ❌ versus.test.ts > two phones can finish a match", 'Error: expected "YOU WIN" to be visible'])

    def test_shouted_headers_read_as_sentences(self):
        self.assertEqual(plain_title("WRITE THE GAME RULES"), "Write the game rules")
        self.assertEqual(plain_title("Preparing git branch: ai/x..."), "Preparing git branch: ai/x")


class LiveTests(unittest.TestCase):
    def test_times_and_what_the_running_step_is_doing(self):
        now = [1000.0]
        timeline = RunTimeline(clock=lambda: now[0])
        timeline.feed((H("⚙️ SUB-TASK 1/1: ADD SCORE") + "\n      - Running LLM (claude-sonnet-4-6, timeout=60s)...\n").encode())
        now[0] += 20
        view = timeline.view(running=True)
        step = view["steps"][-1]
        self.assertEqual((step["title"], step["status"], step["seconds"]), ("Add score", "running", 20))
        self.assertEqual(step["events"][0], {"model": "claude-sonnet-4-6", "status": "running", "reason": "", "seconds": 20})
        timeline.feed(b"        [output] Received 10 chars in 19.8s\n      - Wrote src/score.ts\n")
        self.assertEqual(timeline.view()["steps"][-1]["last"], "Wrote src/score.ts")  # never the raw model line
        timeline.feed(b"partial line without newline")  # held until its end arrives
        self.assertNotIn("partial line without newline", timeline.lines(step["id"]))


class EndpointTests(ServerTestCase):
    def test_a_live_runs_timeline_and_a_steps_output(self):
        session = self.server.sessions.start("check", "Setup check", [sys.executable, "-c", f"print({LOG!r})"], self.root,
                                             self.server.child_env(), ui.runtime_dir(self.root) / "logs" / "ui")
        for _ in range(100):
            if not session.running:
                break
            time.sleep(0.05)
        time.sleep(0.1)
        res, data = self.request("GET", f"/api/runs/{session.id}/timeline")
        self.assertEqual(res.status, 200)
        self.assertIn("Write the game rules", [s["title"] for s in data["steps"]])
        self.assertFalse(data["running"])
        tests = next(s for s in data["steps"] if s["title"] == "Running tests")
        _, lines = self.request("GET", f"/api/runs/{session.id}/timeline?step={tests['id']}")
        self.assertIn('Error: expected "YOU WIN" to be visible', lines["lines"])

    def test_a_saved_run_is_read_back_from_its_log(self):
        folder = ui.runtime_dir(self.root) / "logs" / "ui"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "20261110-120000-abc123-execute.log").write_text(LOG + "\n[exited with code 1]\n")
        res, data = self.request("GET", "/api/runs/20261110-120000-abc123/timeline")
        self.assertEqual(res.status, 200, data)
        self.assertEqual(data["steps"][-1]["status"], "failed")
        self.assertIsNone(data["steps"][0]["seconds"])  # a log has no times
        res, _ = self.request("GET", "/api/runs/20991231-000000-nope00/timeline")
        self.assertEqual(res.status, 404)


class PageTests(unittest.TestCase):
    def test_the_run_page_has_a_timeline_and_keeps_the_terminal(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        page = app[app.index("pages.run = async"):][:4000]
        for bit in ('id="next-step"', 'data-run-tab="timeline"', 'data-run-tab="terminal"', 'id="run-timeline"', 'id="term-wrap" hidden',
                    "wireRunTimeline(id", "attachTerminal(id)"):
            self.assertIn(bit, page)
        tabs = app[app.index("function wireRunTabs"):][:1800]
        self.assertIn('if (r?.waiting && !askedBefore) { askedBefore = true; show("terminal");', tabs)  # a question goes to the terminal


if __name__ == "__main__":
    unittest.main()
