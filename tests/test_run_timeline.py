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

    def test_adaptive_planning_headers_and_spec_gate_signals_tracked_in_timeline(self):
        adaptive_log = "\n".join([
            H("🧭 SPEC SYNTHESIS"),
            '[spec] {"stage": "Pass A · Spec synthesis", "depth": "sparse"}',
            "      - Running LLM (claude-sonnet-4-6, timeout=600s)...",
            "        [output] Received 1200 chars in 12.1s",
            H("⚖️ RISK ASSESSMENT"),
            '[spec] {"stage": "Pass A · Risk & ambiguity assessment", "depth": "sparse"}',
            H("🚦 INTENT GATE: PASSED"),
            '[gate] {"state": "Intent gate · passed (assume_default)"}',
            H("📐 EXECUTION PLANNING"),
            '[spec] {"stage": "Pass B · Execution planning", "depth": "sparse"}',
            "      - Running LLM (claude-sonnet-4-6, timeout=600s)...",
            "        [output] Received 2400 chars in 18.4s",
            H("🛡️ PLAN GATE: AUTO-PASSED"),
            '[gate] {"state": "Plan gate · auto-passed"}',
            H("🔄 REPLANNING"),
            '[spec] {"stage": "Mid-run replanning", "depth": "detailed"}',
            H("🔍 ANALYZING FEEDBACK"),
            '[experience] {"version": "0.2", "impact": "behavior_change · update_acceptance_criteria_and_code"}',
            H("✨ REFINING"),
            H("🧹 REFACTORING"),
            H("⏪ REVERTING"),
            H("🎮 PREVIEWING"),
        ])
        timeline = from_log(adaptive_log + "\n[exited]")
        steps = timeline.view(running=False)["steps"]
        self.assertEqual([s["title"] for s in steps], [
            "Synthesizing feature spec",
            "Assessing ambiguity & risk",
            "Evaluating intent gate: Passed",
            "Planning execution tasks",
            "Validating plan gate: Auto-passed",
            "Replanning remaining tasks",
            "Analyzing feedback impact",
            "Refining from user feedback",
            "Consolidating & refactoring",
            "Reverting last iteration",
            "Generating interactive preview",
        ])
        self.assertEqual([s["kind"] for s in steps], ["plan", "plan", "plan", "plan", "test", "plan", "plan", "code", "code", "git", "review"])
        self.assertEqual(steps[0]["last"], "Pass A · Spec synthesis (sparse input)")
        self.assertEqual(steps[2]["last"], "Intent gate · passed (assume_default)")
        self.assertEqual(steps[4]["last"], "Plan gate · auto-passed")
        self.assertEqual(steps[5]["last"], "Mid-run replanning (detailed input)")
        self.assertEqual(steps[6]["last"], "v0.2 · behavior_change · update_acceptance_criteria_and_code")
        # Raw [spec], [gate], and [experience] JSON lines must not pollute step lines
        for step in steps:
            for line in timeline.lines(step["id"]):
                self.assertFalse(line.startswith(("[spec]", "[gate]", "[experience]")))

    def test_server_run_timeline_breaks_into_steps_and_ends_on_done_step(self):
        server_log = "\n".join([
            "Start OpenDAoC-Core …",
            "  Running: docker compose up -d  (in .)",
            "  Container opendaoc-server Started",
            "  Container opendaoc-db Started",
            "  Waiting for every service to be up and healthy …",
            "Done: OpenDAoC-Core is running.",
        ])
        timeline = from_log(server_log + "\n[exited]")
        steps = timeline.view(running=False)["steps"]
        self.assertEqual([s["title"] for s in steps], [
            "Start OpenDAoC-Core",
            "Waiting for services to be healthy",
            "Done: OpenDAoC-Core is running",
        ])
        self.assertTrue(all(s["status"] == "done" for s in steps))
        self.assertIn("  Container opendaoc-server Started", timeline.lines(steps[0]["id"]))

        # Failure ends on a failed Not done step.
        failed_log = "\n".join([
            "Start OpenDAoC-Core …",
            "  Docker isn't running: starting Docker Desktop …",
            "  Docker is running.",
            "Not done: OpenDAoC-Core is unhealthy.",
        ])
        failed_steps = from_log(failed_log + "\n[exited with code 1]").view(running=False)["steps"]
        self.assertEqual([s["title"] for s in failed_steps], [
            "Start OpenDAoC-Core",
            "Starting Docker Desktop",
            "Not done: OpenDAoC-Core is unhealthy",
        ])
        self.assertEqual(failed_steps[-1]["status"], "failed")
        self.assertEqual(failed_steps[-1]["errors"], 1)

        # Generic single-step run without markers resolves from Starting -> Done on exit 0.
        plain_steps = from_log("ok line\n[exited]").view(running=False)["steps"]
        self.assertEqual([(s["title"], s["status"]) for s in plain_steps], [("Done", "done")])


class LiveTests(unittest.TestCase):
    def test_quota_handoff_keeps_the_fallback_answer_on_the_right_model(self):
        for output in ([], ["[output] Received 0 chars in 1s"]):
            with self.subTest(output=output):
                timeline = RunTimeline()
                for line in (
                    "Running LLM (first, timeout=60s)...",
                    *output,
                    "⏸️  first is out of usage until 11:00 PM.",
                    "Running LLM (second, timeout=60s)...",
                    "[output] Received 25 chars in 2s",
                ):
                    timeline.line(line)

                step = timeline.view()["steps"][-1]
                self.assertEqual(step["status"], "running")
                self.assertEqual(step["errors"], 0)
                self.assertEqual(
                    [(e["model"], e["status"], e["reason"]) for e in step["events"]],
                    [("first", "quota", "out of usage until 11:00 PM"), ("second", "done", "")],
                )

    def test_task_markers_accumulate_without_adding_terminal_steps(self):
        from orchestrator.job_runs import task_attempt_marker, task_marker

        timeline = RunTimeline()
        first, second = ({"run_history_id": char * 32} for char in ("a", "b"))
        marker = (task_marker("j1", [first, second]) + "\r\n").encode()
        timeline.feed(marker[:20])
        timeline.feed(marker[20:])
        timeline.feed((task_marker("j1", [first]) + "\n").encode())
        attempt = (task_attempt_marker("j1", first, "passed", "Changed app.py") + "\r\n").encode()
        timeline.feed(attempt[:31])
        timeline.feed(attempt[31:])
        self.assertEqual(timeline.task_refs, {"j1": {"a" * 32, "b" * 32}})
        self.assertEqual(timeline.task_attempts["j1"]["a" * 32], {"status": "passed", "detail": "Changed app.py"})
        self.assertEqual([step["title"] for step in timeline.steps], ["Starting"])
        self.assertEqual(timeline.current["lines"], 0)

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
    def test_job_run_endpoint_returns_only_links_for_recorded_tasks(self):
        from orchestrator.job_runs import record

        job_id = "20260922-bug-1"
        path = ui.resolve_job_path(self.root, job_id)
        job = ui.read_json_file(path)
        task_id = "a" * 32
        job.setdefault("plan", {})["tasks"] = [{"title": "Fix", "run_history_id": task_id}, {"title": "Check"}]
        ui.write_json_file(path, job)
        runtime = ui.runtime_dir(self.root)
        record(runtime, {"id": "linked", "job": job_id, "action": "execute", "task_refs": {job_id: [task_id]}})
        record(runtime, {"id": "unlinked", "job": job_id, "action": "revise"})
        response, data = self.request("GET", f"/api/jobs/{job_id}/runs")
        self.assertEqual(response.status, 200)
        self.assertEqual(data["task_run_ids"], [["linked"], []])
        self.assertEqual({run["id"] for run in data["job_runs"]}, {"linked", "unlinked"})

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
        page = app[app.index("pages.run = async"):][:5000]
        for bit in ('id="next-step"', 'data-run-tab="timeline"', 'data-run-tab="terminal"', 'id="run-timeline"', 'id="term-wrap" hidden',
                    "wireRunTimeline(id", "attachTerminal(id)"):
            self.assertIn(bit, page)
        tabs = app[app.index("function wireRunTabs"):][:1800]
        self.assertIn('if (r?.waiting && !askedBefore) { askedBefore = true; show("terminal");', tabs)  # a question goes to the terminal

    def test_failed_run_auto_opens_steps_with_errors(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        timeline = app[app.index("function wireRunTimeline"):app.index("function wireRunTabs")]
        self.assertIn('s.status === "failed" || s.errors > 0', timeline)

    def test_timeline_omits_top_running_duration_and_step_count_banner(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        timeline = app[app.index("function wireRunTimeline"):][:2500]
        self.assertNotIn("run-timeline-meta", timeline)
        family = app[app.index("function runFamilyHtml"):app.index("function runHeader")]
        self.assertNotIn('<span class="pill neutral">Run</span>', family)

    def test_run_family_subtitle_wraps_instead_of_bleeding_offscreen(self):
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn(".run-family > span, .run-family > a { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }", css)
        mobile = css[css.index('@media (max-width: 760px) {\n  html:has(body[data-page="run"])'):][:2500]
        sub_block = mobile[mobile.index('body[data-page="run"] #page-sub {'):][:350]
        self.assertIn("white-space: normal;", sub_block)
        self.assertIn("overflow-wrap: anywhere;", sub_block)

    def test_job_detail_and_timeline_step_lists_highlight_current_step(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        # 1. Job lifecycle stepper highlights the active/current step across tones
        for snippet in (
            '.job-step.active,\n.job-step[aria-current="step"] {',
            "background: var(--accent-soft);",
            "border-color: var(--accent);",
            ".job-step.active.attention {",
            ".job-step.active.failed {",
            ".job-step.active.done {",
        ):
            self.assertIn(snippet, css)
        # 2. Job detail Tasks / Steps list highlights the current task row and badge
        for snippet in (
            'class="item task-row${isCurrent ? " is-current" : ""}"${isCurrent ? \' aria-current="step"\' : ""}',
            'task-current-badge',
        ):
            self.assertIn(snippet, app)
        for snippet in (
            ".task-row.is-current { background: var(--accent-soft); border-left: 3px solid var(--accent); }",
            ".task-number.is-current { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }",
        ):
            self.assertIn(snippet, css)
        # 3. Live run step timeline highlights the running step
        self.assertIn('${running ? \' aria-current="step"\' : ""}', app)
        self.assertIn(".run-step.is-running .run-step-main {", css)

    def test_doc_download_uses_icon_in_upper_right_header(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn('id="doc-download" aria-label="Download .md" title="Download .md"><svg class="icon" aria-hidden="true"><use href="#i-download"/></svg></button>', app)
        self.assertIn('id="proj-docs-preview-download" aria-label="Download .md" title="Download .md"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-download"/></svg></button>', app)
        self.assertIn(".topbar:has(> .topbar-actions > #doc-download)", css)
        self.assertIn(".topbar:has(> .topbar-actions > :is(details.more, .btn.icon-only):only-child)", css)

    def test_job_failure_copy_log_icon_sits_next_to_details(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        html = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "index.html").read_text()
        self.assertIn('<symbol id="i-log"', html)
        self.assertIn('id="copy-job-failure-log"', app)
        self.assertIn('<use href="#i-log"/>', app)
        self.assertIn('<div class="job-hero-head">${heroTitleHtml}${copyFailureBtnHtml}</div>', app)
        self.assertIn('toast("Failure details and logs copied")', app)
        self.assertIn('btn.classList.add("is-copied")', app)
        self.assertIn('<svg class="icon" aria-hidden="true"><use href="#i-check"/></svg><span>Copied</span>', app)
        self.assertIn(".job-hero-head {", css)
        self.assertIn(".hero-copy-log-btn {", css)
        self.assertIn(".hero-copy-log-btn.is-copied {", css)
        self.assertIn("const steer = activeRun || s.question || reviewingPlan || signIn || diskBlocked || s.state.blocker || autoStopped ? null", app)
        self.assertIn('const showNextAction = next && !diskBlocked && !(signInAction && next.action === "debug");', app)

    def test_job_and_list_titles_wrap_up_to_two_lines_before_clamping(self):
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        job_title_rule = css[css.index(".job-table-row .job-title {"):css.index("}", css.index(".job-table-row .job-title {"))]
        self.assertIn("-webkit-line-clamp: 2;", job_title_rule)
        self.assertIn("line-clamp: 2;", job_title_rule)
        self.assertIn("white-space: normal;", job_title_rule)
        item_title_line = next(line for line in css.splitlines() if line.startswith(".item .title {"))
        self.assertIn("-webkit-line-clamp: 2;", item_title_line)
        self.assertIn("line-clamp: 2;", item_title_line)

    def test_spinner_and_hook_noise_do_not_clobber_ai_activity_updates(self):
        from orchestrator.web.run_progress import RunProgress

        sample = "\n".join([
            H("🤖 AI THINKING"),
            "      - Running LLM (gpt-6.2-astra, timeout=1200s)...",
            "        [stderr] hook: PreToolUse",
            "        [stderr] mcp: codebase-memory-mcp/search_code started",
            "        [stderr] mcp: codebase-memory-mcp/search_code (completed)",
            "        [stderr] ==> Generating the Xcode project",
            "        [stderr] Importing 'GUID: 86b9e99330e7f49a0a29c4ef344d2426 - Path: Assets/Resources/Tree1.png' with importer",
            "        [stderr] hook: PostToolUse",
            "        [stderr] hook: PostToolUse Completed",
            "⠹ Thinking (Active stderr)... (Ctrl-C to cancel, 19m 52s)",
            "        [waiting] gpt-6.2-astra: 20m so far, last output 3s ago",
        ])
        tl = RunTimeline()
        tl.feed(sample.encode("utf-8"))
        step = tl.view(running=True)["steps"][-1]
        self.assertEqual(step["last"], "Importing Assets/Resources/Tree1.png")
        self.assertEqual(step["recent"], [
            "MCP: search_code",
            "Generating the Xcode project",
            "Importing Assets/Resources/Tree1.png",
        ])

        rp = RunProgress()
        rp.feed(sample.encode("utf-8"))
        self.assertEqual(rp.snapshot()["activity"], "Importing Assets/Resources/Tree1.png")


if __name__ == "__main__":
    unittest.main()
