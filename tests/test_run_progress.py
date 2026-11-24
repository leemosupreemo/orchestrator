"""Live run status follows worker output without depending on the terminal buffer."""
import subprocess
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.web.run_progress import RunProgress


class RunProgressTests(unittest.TestCase):
    def test_planning_milestones_advance_before_the_next_numbered_step(self):
        progress = RunProgress()
        progress.feed(b"[1/3] Planning feature\n")
        self.assertEqual(progress.snapshot()["step_fraction"], 0)
        progress.feed(b'[spec] {"stage":"Pass A: Spec synthesis","label":"Synthesizing feature spec"}\n')
        progress.feed(b'[spec] {"stage":"Pass A: Risk assessment","label":"Assessing ambiguity & risk"}\n')
        self.assertEqual(progress.snapshot()["step_fraction"], 0.2)
        progress.feed(b'[gate] {"state":"Intent gate passed","label":"Evaluating intent gate"}\n')
        self.assertEqual(progress.snapshot()["step_fraction"], 0.4)
        progress.feed(b'[spec] {"label":"Planning execution tasks"}\n[gate] {"state":"Plan gate passed"}\n')
        self.assertEqual(progress.snapshot()["step_fraction"], 0.8)
        progress.feed(b'[spec] {"label":"Assessing ambiguity & risk"}\n')
        self.assertEqual(progress.snapshot()["step_fraction"], 0.8)
        progress.feed(b"[2/3] Creating job\n")
        self.assertEqual((progress.snapshot()["step"], progress.snapshot()["step_fraction"]), (2, 0))

    def test_build_and_test_phases_advance_only_within_the_current_task(self):
        progress = RunProgress()
        progress.feed(b"=== SUB-TASK 1/2: Add login ===\n=== AI THINKING ===\n")
        self.assertEqual(progress.snapshot()["step_fraction"], 0)
        progress.feed(b"=== BUILDING ===\n")
        self.assertEqual(progress.snapshot()["step_fraction"], 0.5)
        progress.feed(b"=== TESTING ===\n[2/2] Child test progress\n")
        self.assertEqual((progress.snapshot()["step"], progress.snapshot()["step_fraction"]), (1, 0.75))
        progress.feed(b"=== SUB-TASK 2/2: Add tests ===\n")
        self.assertEqual((progress.snapshot()["step"], progress.snapshot()["step_fraction"]), (2, 0))

    def test_observed_ai_activity_advances_step_fraction_monotonically_within_phase(self):
        progress = RunProgress()
        progress.feed(b"=== SUB-TASK 1/2: Tune habitats ===\n=== AI THINKING ===\n")
        self.assertEqual(progress.snapshot()["step_fraction"], 0)
        # Spinner noise does not move progress.
        progress.feed("⠹ Thinking (Active stderr)... (Ctrl-C to cancel, 5m)\n".encode("utf-8"))
        self.assertEqual(progress.snapshot()["step_fraction"], 0)
        # Distinct observed activity moves progress smoothly toward 0.45 without crossing BUILDING (0.5).
        progress.feed(b"[stderr] mcp: codebase-memory-mcp/search_code started\n")
        first = progress.snapshot()["step_fraction"]
        self.assertGreater(first, 0)
        self.assertLess(first, 0.45)
        for i in range(20):
            progress.feed(f"[stderr] Importing 'GUID: 000000000000000000000000000000{i:02x} - Path: Assets/Tree{i}.png'\n".encode("utf-8"))
        after_many = progress.snapshot()["step_fraction"]
        self.assertGreater(after_many, first)
        self.assertLessEqual(after_many, 0.45)
        # Entering BUILDING jumps cleanly to 0.5 and stays monotonic.
        progress.feed(b"=== BUILDING ===\n")
        self.assertEqual(progress.snapshot()["step_fraction"], 0.5)

    def test_a_task_batch_can_show_progress_before_the_build_finishes(self):
        progress = RunProgress()
        progress.feed(b"=== SUB-TASK 1-2/3: Build the related changes ===\n=== BUILDING ===\n")
        status = progress.snapshot()
        self.assertEqual((status["step"], status["total"], status["step_fraction"]), (1, 3, 0.5))
        progress.feed(b"=== SKIPPING TESTS ===\n")
        self.assertEqual(progress.snapshot()["step_fraction"], 0.75)

    def test_long_task_headers_use_the_real_terminal_padding(self):
        from orchestrator.scripts.common import print_phase

        output = io.StringIO()
        with patch('os.get_terminal_size', return_value=os.terminal_size((80, 24))), contextlib.redirect_stdout(output):
            print_phase('sub-task 2/10', subtext='Implement the sign-in flow and session persistence across launches' * 2)
        progress = RunProgress()
        progress.feed(output.getvalue().encode())
        self.assertEqual((progress.snapshot()['step'], progress.snapshot()['total']), (2, 10))

    def test_coverage_plan_reports_suites_and_the_one_being_written(self):
        progress = RunProgress()
        progress.feed(b'[suites] ["tests/test_auth.py", "tests/test_cart.py", "tests/test_sync.py"]\n')
        progress.feed(b'=== PLANNING ===\nplanning mentions test_sync.py\n')
        snapshot = progress.snapshot()
        self.assertEqual((snapshot["suites"], snapshot["suite"]), (["tests/test_auth.py", "tests/test_cart.py", "tests/test_sync.py"], 0))
        progress.feed(b'=== IMPLEMENTATION ===\nWriting tests/test_auth.py\nWriting tests/test_cart.py\n')
        self.assertEqual(progress.snapshot()["suite"], 2)

    def test_scheduled_machines_are_reported(self):
        progress = RunProgress()
        progress.feed(b'Scheduled job-1 group g1 to mac-mini using model-a\nScheduled job-1 group g2 to local using model-a\n')
        self.assertEqual(progress.snapshot()["machines"], ["mac-mini", "local"])

    def test_the_label_says_what_is_being_waited_on_once_a_model_call_starts(self):
        progress = RunProgress()
        progress.feed("=========================== 📡 SCHEDULING ===========================\n".encode())
        self.assertEqual(progress.snapshot()["label"], "Scheduling worker")
        progress.feed(b"Scheduled job-1 group MAIN to local using gpt-5.4\n      - Running LLM (gpt-5.4, timeout=1200s)...\n")
        self.assertEqual(progress.snapshot()["label"], "Waiting for gpt-5.4")
        progress.feed(b"        [output] Received 0 chars in 3.2s\n")
        self.assertEqual(progress.snapshot()["label"], "Reading the model's answer")
        progress.feed(b"      - Running LLM (claude-opus-4-7, timeout=1200s)...\n")
        self.assertEqual(progress.snapshot()["label"], "Waiting for claude-opus-4-7")
        progress.feed(b"=== IMPLEMENTATION ===\n      - Running LLM (claude-opus-4-7, timeout=1200s)...\n")
        self.assertEqual(progress.snapshot()["label"], "Implementing changes")  # a real phase is never overwritten
        progress.feed(b"=== AI THINKING ===\n")
        self.assertEqual(progress.snapshot()["label"], "Working with AI")
        progress.feed(b"      - Running LLM (claude-sonnet-4-6, timeout=1200s)...\n")
        self.assertEqual(progress.snapshot()["label"], "Waiting for claude-sonnet-4-6")

    def test_calls_in_flight_report_how_long_they_have_run_and_their_timeout(self):
        progress = RunProgress()
        progress.feed(b"      - Running LLM (claude-opus-4-7, timeout=1200s)...\n")
        with patch("orchestrator.web.run_progress.time.time", return_value=progress.calls[0]["since"] + 245):
            call = progress.snapshot()["calls"][0]
        self.assertEqual((call["model"], call["age"], call["timeout"]), ("claude-opus-4-7", 245, 1200))
        self.assertEqual(progress.snapshot(running=False)["calls"], [])
        progress.feed(b"        [output] Received 23068 chars in 532.2s\n")
        self.assertEqual(progress.snapshot()["calls"], [])

    def test_models_that_failed_are_listed_with_a_short_reason(self):
        progress = RunProgress()
        progress.feed((
            "      - Running LLM (gpt-5.4, timeout=1200s)...\n        [output] Received 0 chars in 3.2s\n"
            "⚠️  gpt-5.4 failed (ERROR: {\"type\":\"error\",\"status\":400,\"error\":{\"message\":\"The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.\"}}). Attempting fallback...\n"
            "      - Running LLM (o1, timeout=1200s)...\n        [output] Received 0 chars in 0.0s\n"
            "⚠️  o1 failed (o1 failed (code 127):). Attempting fallback...\n"
            "      - Running LLM (slow-model, timeout=60s)...\n"
            "⚠️  slow-model timed out. Attempting fallback...\n").encode())
        failed = {f["model"]: f["reason"] for f in progress.snapshot()["failed"]}
        self.assertEqual(failed["gpt-5.4"], "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account")
        self.assertEqual(failed["o1"], "couldn't start (is it installed?)")
        self.assertEqual(failed["slow-model"], "timed out")
        self.assertEqual(progress.snapshot()["calls"], [])

    def test_stall_notices_are_counted_and_cleared_when_the_model_answers(self):
        progress = RunProgress()
        progress.feed("      - Running LLM (claude-opus-4-7, timeout=1200s)...\n💡 [Stall Notice] No output received for 4m (waiting on remote model/process).\n".encode())
        self.assertEqual(progress.snapshot()["stall"], 4)
        progress.feed(b"        [output] Received 100 chars in 300.0s\n")
        self.assertEqual(progress.snapshot()["stall"], 0)

    def test_status_bar_cursor_restore_does_not_hide_model_completion(self):
        progress = RunProgress()
        progress.feed(b'      - Running LLM (model-a, timeout=300s)...\n')
        progress.feed(b'\x1b7\x1b[21;1H\x1b[K\x1b8        [output] Received 120 chars in 3.0s\n')
        self.assertEqual(progress.snapshot()['agents'], 0)

    def test_session_summary_exposes_progress_after_the_command_exits(self):
        from orchestrator.web.server import PtySession

        with tempfile.TemporaryDirectory() as root:
            session = PtySession("progress-test", "execute", "Build", [sys.executable, "-c",
                "print('=== PLANNING ==='); print('      - Running LLM (model-a, timeout=300s)...')"], Path(root), {})
            deadline = time.monotonic() + 5
            offset = 0
            while session.running and time.monotonic() < deadline:
                offset, _, _ = session.read(offset, 0.1)
            if session.running:
                session.stop()
                self.fail("Fixture command did not finish")
            summary = session.summary()
            self.assertEqual(summary["progress"]["label"], "Planning implementation")
            self.assertEqual(summary["progress"]["models"], ["model-a"])
            self.assertEqual(summary["progress"]["agents"], 0)

    def test_error_summary_ignores_exited_status_line(self):
        from orchestrator.web.server import PtySession

        with tempfile.TemporaryDirectory() as root:
            session = PtySession("error-test", "coverage", "Measure coverage", [sys.executable, "-c",
                "import sys; print('Coverage measurement failed: no tests found'); sys.exit(1)"], Path(root), {})
            deadline = time.monotonic() + 5
            offset = 0
            while session.running and time.monotonic() < deadline:
                offset, _, _ = session.read(offset, 0.1)
            summary = session.summary()
            self.assertEqual(summary["error_summary"], "Coverage measurement failed: no tests found")
            self.assertNotIn("exited with code", summary["error_summary"])
            self.assertNotIn("exited with code", summary["last_line"])

    def test_error_summary_ignores_package_checkout_progress(self):
        from orchestrator.web.server import PtySession

        with tempfile.TemporaryDirectory() as root:
            session = PtySession("error-test", "visual_check", "Simulator check", [sys.executable, "-c",
                "print('Checking out ‘interop-ios-for-google-sdks’ @ 101.0.0 Checking out ‘onboarder’ @ master (b35003b)'); import sys; sys.exit(1)"], Path(root), {})
            deadline = time.monotonic() + 5
            offset = 0
            while session.running and time.monotonic() < deadline:
                offset, _, _ = session.read(offset, 0.1)
            summary = session.summary()
            self.assertEqual(summary["error_summary"], "Run stopped while resolving Swift package dependencies.")

    def test_fragmented_phase_and_task_headers_survive_noisy_output(self):
        progress = RunProgress()
        output = ("\x1b[1;96m=== ⚙️ SUB-TASK 2/10: ADD LOGIN ===\x1b[0m\r\n"
                  "=== ⚙️ IMPLEMENTATION ===\r\n").encode()
        for byte in output:
            progress.feed(bytes([byte]))
        progress.feed(b"ordinary log output\n" * 10000)
        status = progress.snapshot()
        self.assertEqual((status["step"], status["total"]), (2, 10))
        self.assertEqual(status["label"], "Implementing changes")
        self.assertEqual(status["task"], "Add login")
        progress.feed(b"=== PULL REQUEST ===\n")
        status = progress.snapshot()
        self.assertEqual(status["label"], "Creating pull request")
        self.assertIsNone(status["total"])

    def test_active_agents_follow_completion_timeout_and_fallback(self):
        progress = RunProgress()
        progress.feed(b"=== PLANNING ===\n      - Running LLM (provider/model-a, timeout=300s)...\n")
        self.assertEqual(progress.snapshot()["agents"], 1)
        self.assertEqual(progress.snapshot()["active_models"], ["provider/model-a"])
        progress.feed("⚠️  provider/model-a timed out. Attempting fallback...\n".encode())
        self.assertEqual(progress.snapshot()["agents"], 0)
        progress.feed(b"      - Running LLM (provider/model-b, timeout=300s)...\n")
        progress.feed(b"      - Running LLM (provider/model-b, timeout=300s)...\n")
        self.assertEqual(progress.snapshot()["agents"], 2)
        progress.feed(b"        [output] Received 120 chars in 3.0s\n")
        self.assertEqual(progress.snapshot()["agents"], 1)
        self.assertEqual(progress.snapshot()["models"], ["provider/model-a", "provider/model-b"])
        self.assertEqual(progress.snapshot(running=False)["agents"], 0)

    def test_planning_has_no_invented_total_and_ignores_model_output_headers(self):
        progress = RunProgress()
        progress.feed(b"=== PLANNING ===\n[stderr] === TESTING ===\n")
        status = progress.snapshot()
        self.assertEqual(status["label"], "Planning implementation")
        self.assertIsNone(status["total"])
        self.assertEqual(status["agents"], 0)

    def test_coverage_step_headers_tracked(self):
        progress = RunProgress()
        progress.feed(b"\n=== STEP 1/5: CHECKING COVERAGE ENVIRONMENT ===\n")
        status = progress.snapshot()
        self.assertEqual((status["step"], status["total"]), (1, 5))
        self.assertEqual(status["label"], "Checking coverage environment")
        progress.feed(b"\n=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===\n")
        status = progress.snapshot()
        self.assertEqual((status["step"], status["total"]), (3, 5))
        self.assertEqual(status["label"], "Running test suite with coverage")
        progress.feed(b"\n=== STEP 5/5: COVERAGE MEASUREMENT COMPLETE ===\n")
        status = progress.snapshot()
        self.assertEqual((status["step"], status["total"]), (5, 5))
        self.assertEqual(status["label"], "Coverage measurement complete")

    def test_coverage_step_not_hijacked_by_child_test_output(self):
        progress = RunProgress()
        progress.feed(b"\n=== STEP 1/5: CHECKING COVERAGE ENVIRONMENT ===\n")
        progress.feed(b"\n=== STEP 3/5: RUNNING TEST SUITE WITH COVERAGE ===\n")
        # Subprocess running wizard e2e tests emits bracket lines:
        progress.feed(b"[1/5] Project\n")
        progress.feed(b"[2/5] AI setup\n")
        progress.feed(b"[5/5] Verify and finish\n")
        status = progress.snapshot()
        # Must stay on step 3 of 5, NOT jump to step 5 or step 1
        self.assertEqual((status["step"], status["total"]), (3, 5))
        self.assertEqual(status["label"], "Running test suite with coverage")
        progress.feed(b"\n=== STEP 4/5: GENERATING COVERAGE REPORT ===\n")
        status = progress.snapshot()
        self.assertEqual((status["step"], status["total"]), (4, 5))
        self.assertEqual(status["label"], "Generating coverage report")

    def test_planning_header_prepopulates_model(self):
        progress = RunProgress()
        progress.feed(b"[1/3] Planning feature (Stitch AI Mode: Off) using opencode/nemotron-3-ultra-free...\n")
        self.assertEqual(progress.snapshot()["models"], ["opencode/nemotron-3-ultra-free"])

    def test_adaptive_planning_phases_and_gates_tracked_in_run_progress(self):
        progress = RunProgress()
        progress.feed(b"=== PLANNING ===\n[1/3] Planning feature (Stitch AI Mode: Off) using opencode/nemotron-3-ultra-free...\n")
        progress.feed(b'[spec] {"stage": "Pass A \xc2\xb7 Spec synthesis", "label": "Synthesizing feature spec"}\n')
        snap = progress.snapshot()
        self.assertEqual((snap["step"], snap["total"]), (1, 3))
        self.assertEqual(snap["label"], "Synthesizing feature spec")
        self.assertEqual(snap["spec_stage"], "Pass A · Spec synthesis")

        progress.feed(b'[spec] {"stage": "Pass A \xc2\xb7 Risk & ambiguity assessment", "depth": "sparse", "label": "Assessing ambiguity & risk"}\n')
        snap = progress.snapshot()
        self.assertEqual(snap["label"], "Assessing ambiguity & risk")
        self.assertEqual(snap["spec_stage"], "Pass A · Risk & ambiguity assessment")
        self.assertEqual(snap["planning_depth"], "sparse")

        progress.feed(b'[gate] {"state": "Intent gate \xc2\xb7 passed (assume_default)", "label": "Evaluating intent gate"}\n')
        snap = progress.snapshot()
        self.assertEqual(snap["label"], "Evaluating intent gate")
        self.assertEqual(snap["gate_state"], "Intent gate · passed (assume_default)")

        progress.feed(b'[spec] {"stage": "Pass B \xc2\xb7 Execution planning", "depth": "sparse", "label": "Planning execution tasks"}\n')
        snap = progress.snapshot()
        self.assertEqual(snap["label"], "Planning execution tasks")
        self.assertEqual(snap["spec_stage"], "Pass B · Execution planning")

        progress.feed(b'[gate] {"state": "Plan gate \xc2\xb7 auto-passed"}\n')
        snap = progress.snapshot()
        self.assertEqual(snap["gate_state"], "Plan gate · auto-passed")

        # Also verify direct print_phase headers update label, spec_stage, and gate_state without clearing (step, total)
        progress.feed(b"=== SPEC SYNTHESIS ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["spec_stage"], progress.snapshot()["step"]),
                         ("Synthesizing feature spec", "Pass A · Spec synthesis", 1))
        progress.feed(b"=== RISK ASSESSMENT ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["spec_stage"]),
                         ("Assessing ambiguity & risk", "Pass A · Risk & ambiguity assessment"))
        progress.feed(b"=== INTENT GATE: PASSED ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["gate_state"]),
                         ("Evaluating intent gate", "Passed"))
        progress.feed(b"=== EXECUTION PLANNING ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["spec_stage"]),
                         ("Planning execution tasks", "Pass B · Execution planning"))
        progress.feed(b"=== PLAN GATE: AUTO-PASSED ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["gate_state"]),
                         ("Validating plan gate", "Auto-passed"))
        progress.feed(b"=== REPLANNING ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["spec_stage"]),
                         ("Replanning remaining tasks", "Mid-run replanning"))
        progress.feed(b"=== PREVIEWING ===\n")
        self.assertEqual(progress.snapshot()["label"], "Generating interactive preview")
        progress.feed(b"=== ANALYZING FEEDBACK: BEHAVIOR CHANGE ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["feedback_impact"]),
                         ("Analyzing feedback impact", "Behavior change"))
        progress.feed(b'[experience] {"version": "0.2", "feedback_type": "behavior_change", "impact": "behavior_change \xc2\xb7 update_acceptance_criteria_and_code", "stage": "Experience v0.2 \xc2\xb7 behavior change", "label": "Refining from user feedback"}\n')
        snap = progress.snapshot()
        self.assertEqual(snap["label"], "Refining from user feedback")
        self.assertEqual(snap["experience_version"], "v0.2")
        self.assertEqual(snap["feedback_impact"], "behavior_change · update_acceptance_criteria_and_code")
        self.assertEqual(snap["spec_stage"], "Experience v0.2 · behavior change")
        progress.feed(b"=== REFACTORING ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["spec_stage"]),
                         ("Consolidating & refactoring", "Experience loop · Refactoring"))
        progress.feed(b"=== REVERTING ===\n")
        self.assertEqual((progress.snapshot()["label"], progress.snapshot()["spec_stage"]),
                         ("Reverting last iteration", "Experience loop · Reverting"))


class RunStatusRenderingTests(unittest.TestCase):
    def test_home_job_row_has_no_status_container_and_running_now_shows_step_and_elapsed(self):
        source = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of ['testsClass', 'jobTableRow', 'liveRunCard', 'elapsed', 'runPill']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.pill = (tone, label) => `<span class="pill ${tone}">${label}</span>`;
globalThis.stoppingRuns = new Set();
globalThis.runningMark = '<span class="run-spin" aria-hidden="true"></span>';
globalThis.ago = () => 'just now';
globalThis.jobPill = () => '<span class="pill working">Running</span>';
globalThis.jobKind = (j) => j.kind;
globalThis.jiraChip = () => '';
globalThis.featureChip = () => '';
Date.now = () => 1000000;
const run = {id: 'r1', job: 'job-1', running: true, started: 875, title: 'Build', idle: 0,
  progress: {label: 'Implementing changes', task: 'Add <login>', step: 2, total: 10}};
globalThis.state = {runs: [run]};
const job = {id: 'job-1', title: 'Sign-in', updated: 999, kind: 'Feature', active_run: true};
const row = jobTableRow(job, {number: 2});
assert.doesNotMatch(row, /job-tests/);
assert.match(row, /class="col-job has-num"/);
assert.match(row, /<span class="job-row-num" aria-hidden="true">2<\/span>/);
assert.match(jobTableRow({...job, kind: 'Tests'}), /class="job-table-row job-tests"/);
assert.doesNotMatch(row, /run-status|Step 2 of 10|Click for details/);
assert.match(row, /href="#\/jobs\/job-1"/);
const card = liveRunCard(run);
for (const part of ['Step 2 of 10', '2m elapsed', 'Add &lt;login&gt;', 'live-run-progress']) assert(card.includes(part), part);
assert(!liveRunCard({...run, progress: {}}).includes('Step '));
assert.match(liveRunCard({...run, waiting: true}), /Waiting for you/);
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disconnected_stream_completion_keeps_visual_check_results(self):
        source = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of ['finishRunStatus', 'runFailureGuidance', 'nextStep']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  const prefix = name === 'finishRunStatus' ? 'async ' : '';
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = ${prefix}function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.act = (action) => `data-action="${action}"`;
globalThis.plural = (n, noun) => `${n} ${noun}${n === 1 ? '' : 's'}`;
const panel = {}, keys = {}, input = {};
globalThis.$ = selector => ({'#next-step': panel, '#keybar': keys, '#term-input': input})[selector];
globalThis.current = {page: 'run', args: ['visual']};
globalThis.getVisualCheckForRun = async () => ({id: 'vc1', screenshots: ['screen.png']});
globalThis.runHeader = () => ({});
globalThis.setHeader = globalThis.hydrateAuthImages = globalThis.wireImagePreviews = globalThis.wireRunGuidance = () => {};
(async () => {
  await finishRunStatus({id: 'visual', running: false, action: 'visual_check', title: 'Visual check', exit_code: 0});
  assert.match(panel.innerHTML, /screen\.png/);
  assert.match(panel.innerHTML, /report\.md/);
  assert.equal(keys.hidden, true);
  assert.equal(input.hidden, true);
})().catch(e => { console.error(e); process.exit(1); });
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_live_panel_details_elapsed_waiting_and_terminal_states(self):
        source = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of ['runFailureGuidance', 'runDuration', 'runStatus', 'updateRunStatus', 'nextStep', 'suiteGroupsHtml', 'runHeader']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.plural = (n, word, many = `${word}s`) => `${n} ${Number(n) === 1 ? word : many}`;
globalThis.act = (action) => `data-action="${action}"`;
globalThis.stoppingRuns = new Set();
globalThis.wireRunGuidance = () => {};
globalThis.current = {page: 'run', args: ['r1']};
Date.now = () => 1000000;
const run = {id: 'r1', running: true, started: 875, action: 'execute', title: 'Build', command: 'orchestrator build',
  progress: {label: 'Implementing changes', task: 'Add <login>', step: 2, total: 10,
    active_models: ['provider/model-a'], models: ['provider/model-a'], agents: 1}};
const header = runHeader({...run, command: 'orchestrator script ux_review_run.py pass'});
assert.doesNotMatch(header.sub, /pill|Running/);
assert.equal(header.sub, '');  // the raw command is not shown; the status container carries what matters
const namedRun = {...run, job: 'job-18', job_title: 'Camelot Hills ground cover'};
assert.equal(runHeader(namedRun).title, 'Camelot Hills ground cover');
assert.equal(runHeader({...run, result_job: 'job-19'}, {job: {id: 'job-19', title: 'New feature'}}).title, 'New feature');
assert.doesNotMatch(runStatus(namedRun), /<dt>Job<\/dt>/);
const many = runStatus({...run, progress: {...run.progress,
  assigned_models: ['model-a', 'model-b', 'model-c', 'model-d', 'model-e'], active_models: [], models: []}});
assert.match(many, /model-a, model-b, model-c, and 2 more/);
assert.doesNotMatch(many, /model-d|model-e/);
const manyCalls = runStatus({...run, progress: {...run.progress,
  calls: ['model-a', 'model-b', 'model-c', 'model-d'].map(model => ({model, age: 30}))}});
assert.match(manyCalls, /model-c \(working for 30s\), and 1 more/);
assert.doesNotMatch(manyCalls, /model-d/);
let html = runStatus({...run, summary: 'Improve test coverage toward 80%', progress: {...run.progress, machines: ['mac-mini'], suites: ['tests/a.py', 'tests/b.py'], suite: 2}});
for (const part of ['Improve test coverage toward 80%', 'mac-mini (active)', '2 planned · working on 2 of 2: b.py']) assert(html.includes(part), part);
assert.match(runStatus(run), /Assigning a machine/);
html = runStatus(run);
for (const part of ['Step 2 of 10', 'Implementing changes', 'provider/model-a', '(working)', '2m 5s', 'run-status-pct', '10%']) assert(html.includes(part), part);
assert.match(html, /<strong class="run-status-step">Step 2 of 10<\/strong>/);
assert.doesNotMatch(html, /Add &lt;login&gt;|run-status-task/);
const first = runStatus({...run, progress: {step: 1, total: 3, step_fraction: 0.4}});
assert.match(first, /Step 1 of 3/);
assert.match(first, /value="13" max="100"/);
assert.match(first, /run-status-pct">13%/);
const last = runStatus({...run, progress: {step: 100, total: 100, step_fraction: 0.9}});
assert.match(last, /run-status-pct">99%/);
const slow = runStatus({...run, progress: {label: 'Waiting for claude-opus-4-7', agents: 1, models: ['gpt-5.4', 'claude-opus-4-7'], active_models: ['claude-opus-4-7'],
  calls: [{model: 'claude-opus-4-7', age: 485, timeout: 1200}],
  failed: [{model: 'gpt-5.4', reason: "Not supported <here>"}, {model: 'o1', reason: "couldn't start (is it installed?)"}]}});
for (const part of ['Waiting for claude-opus-4-7', 'claude-opus-4-7 (working for 8m)', '2 models couldn&#39;t run', 'Not supported &lt;here&gt;']) assert(slow.includes(part) || slow.includes(part.replace('&#39;', "'")), part);
assert.doesNotMatch(slow, /data-run-slow-hint|Reasoning models often take/);
const fresh = runStatus({...run, progress: {label: 'Waiting for m', agents: 1, models: ['m'], active_models: ['m'], calls: [{model: 'm', age: 30, timeout: 600}], failed: []}});
assert(fresh.includes('m (working for 30s)'));
assert.doesNotMatch(fresh, /run-slow-hint|Skipped models/);  // nothing to say until it has been quiet a while, or something was skipped
assert.doesNotMatch(runStatus({...run, running: false, exit_code: 0, progress: {...run.progress, calls: [{model: 'm', age: 500, timeout: 600}]}}), /run-slow-hint/);
const flagged = runStatus({...run, running: false, exit_code: 0, progress: {label: 'Reading the model\'s answer', models: ['gpt-5.4', 'claude-opus-4-7'], failed: [{model: 'gpt-5.4', reason: 'Not supported'}]},
  result: {headline: '4 test files added with 49 tests, not checked yet', details: ['Location: Tests/'], tone: 'warn', partial: true}});
for (const part of ['Partially complete', '4 test files added with 49 tests', 'Location: Tests/', 'claude-opus-4-7']) assert(flagged.includes(part), part);
assert.doesNotMatch(flagged, /Reading the model|<dd>gpt-5.4/);  // not where it was, and not the model that couldn't run
assert.match(runStatus({...run, running: false, exit_code: 0, progress: {label: 'Scheduling worker'}}), /Job execution complete|Run complete/);  // a finished run is never headlined with where it started
assert.match(runStatus({...run, waiting: true}), /Waiting for you/);
assert.match(runStatus({...run, stopping: true}), /Stopping/);
const planning = runStatus({...run, progress: {label: 'Planning implementation', agents: 0, models: [], active_models: []}});
assert.match(planning, /Planning implementation/);
assert.doesNotMatch(planning, /Step .* of|undefined|NaN/);
assert.match(runStatus({...run, running: false, exit_code: 1}), /banner failed/);
assert.match(runStatus({...run, running: false, exit_code: 0}), /banner done/);
const stoppedJobRun = runStatus({...run, job: 'job-1', action: 'execute', running: false, stopped: true});
assert.match(stoppedJobRun, /Run was stopped/);
assert.match(stoppedJobRun, /Resume/);
assert.doesNotMatch(stoppedJobRun, /Open job/);
const completedCov = runStatus({...run, action: 'coverage', running: false, exit_code: 0, progress: {step: 5, total: 5, label: 'Coverage measurement complete'}});
assert.match(completedCov, /pill done/);
assert.match(completedCov, /Completed/);
assert.match(completedCov, /Step 5 of 5/);
assert.match(completedCov, /View coverage/);
assert.match(completedCov, /100%/);
assert.match(completedCov, /Completed \(exit 0\)/);
assert.match(completedCov, /None \(local run\)/);
assert.match(html, /<dt>Models<\/dt>/);
assert.match(runStatus({...run, action: 'coverage', progress: {step: 1, total: 5}}), /None \(local run\)/);
assert.match(runStatus({...run, progress: {label: 'Building', models: ['provider/model-a'], active_models: []}}), /provider\/model-a \(idle\)/);
assert.match(runStatus({...run, progress: {label: 'Building', assigned_models: ['provider/model-b'], models: [], active_models: []}}), /provider\/model-b \(assigned\)/);

// Failure guidance and action buttons
const failedCov = runStatus({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "No Swift source files or Package.swift manifest found yet"});
assert.match(failedCov, /banner failed/);
assert.match(failedCov, /run-status-notice/);
assert.match(failedCov, /No source files or tests yet/);
assert.doesNotMatch(failedCov, /Next steps to fix/);
assert.doesNotMatch(failedCov, /<dt>Models<\/dt>/);
assert.doesNotMatch(failedCov, /Elapsed/);
assert.doesNotMatch(failedCov, /Measuring code coverage failed/);
assert.match(failedCov, /Add tests/);
assert.match(failedCov, /href="#\/new\?type=coverage"/);
assert.match(failedCov, /href="#\/tests"/);

globalThis.current = {page: 'tests'};
const failedCovTestsPage = runStatus({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "No Swift source files or Package.swift manifest found yet"});
assert.doesNotMatch(failedCovTestsPage, /Back to Tests/);
assert.doesNotMatch(failedCovTestsPage, /href="#\/tests"/);
globalThis.current = undefined;

const failedNoTests = runStatus({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "No source files or tests found yet in this project. Create initial code and tests before measuring coverage."});
assert.match(failedNoTests, /No source files or tests yet/);
assert.match(failedNoTests, /Add tests/);

const failedExited = runStatus({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "[exited with code 1]"});
assert.match(failedExited, /No source files or tests yet/);
assert.match(failedExited, /Add tests/);

const failedPyCov = runStatus({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "pip install coverage required"});
assert.match(failedPyCov, /coverage\.py required/);
const retryCount = (failedPyCov.match(/Retry calculation/g) || []).length;
assert.equal(retryCount, 1, "There must be exactly one Retry calculation button, no duplicates");

const failedCovTests = runStatus({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "Coverage measurement failed because test suite had failing tests"});
assert.match(failedCovTests, /Tests failed during coverage/);
assert.match(failedCovTests, /Fix tests with AI/);
assert.match(failedCovTests, /data-action="new_job"/);
assert.doesNotMatch(failedCovTests, /href="#\/new\?type=bug/);
assert.doesNotMatch(failedCovTests, /Run failed/);
assert.doesNotMatch(failedCovTests, /failed<\/strong>/);
const standaloneFailedCov = runStatus({...run, action: 'coverage', progress: null, running: false, exit_code: 1, error_summary: "Coverage measurement failed because test suite had failing tests"});
assert.match(standaloneFailedCov, /Coverage measurement/);
assert.doesNotMatch(standaloneFailedCov, /Run failed/);
assert.equal(runDuration(1000, 1025), "25s");
assert.equal(runDuration(1000, 1120), "2m 0s");

const failedBuild = runStatus({...run, action: 'build', running: false, exit_code: 1, error_summary: "compilation error: unresolved identifier"});
assert.match(failedBuild, /Build compilation failed/);
assert.match(failedBuild, /Fix build with AI/);
assert.match(failedBuild, /data-action="build"/);

const failedCheck = runStatus({...run, action: 'check_config', running: false, exit_code: 1, error_summary: "Invalid config"});
assert.match(failedCheck, /Configuration check reported issues/);
assert.match(failedCheck, /Open Settings/);

const failedThinking = runStatus({...run, action: 'ux_pass', running: false, exit_code: 1, error_summary: "Thinking: Deep Reasoning model active... (3m 22s)"});
assert.match(failedThinking, /Run interrupted while model was thinking/);
assert.match(failedThinking, /data-auto-recover/);
assert.match(failedThinking, /data-cancel-autorecover/);

const nextFailed = nextStep({...run, action: 'coverage', running: false, exit_code: 1, error_summary: "No Swift source files or Package.swift manifest found yet"});
assert.match(nextFailed, /banner failed/);
assert.match(nextFailed, /No source files or tests yet/);
assert.match(nextFailed, /Add tests/);

const emptySuites = suiteGroupsHtml([]);
assert.match(emptySuites, /No test suites found in this project/);
assert.match(emptySuites, /Add tests/);
assert.match(emptySuites, /data-action="add_tests"/); // one click on the Tests page: the job starts without the form

let replacements = 0;
const clock = {textContent: ''};
const panel = {set innerHTML(value) { replacements++; this.html = value; },
  querySelector: selector => selector === '[data-run-elapsed]' ? clock : {}};
globalThis.$ = selector => selector === '#next-step' ? panel : {};
updateRunStatus(run);
Date.now = () => 1001000;
updateRunStatus(run);
assert.equal(replacements, 1, 'Elapsed updates must preserve the status announcement');
assert.equal(clock.textContent, '2m 6s');
updateRunStatus({...run, waiting: true});
assert.equal(replacements, 2);
assert.match(panel.html, /Waiting for you/);
updateRunStatus({...run, running: false, exit_code: 1});
assert.match(panel.html, /banner failed/);

// Adaptive specification & planning statuses in status container
for (const phaseLabel of [
  'Synthesizing feature spec',
  'Assessing ambiguity & risk',
  'Evaluating intent gate',
  'Planning execution tasks',
  'Validating plan gate',
  'Replanning remaining tasks',
  'Generating interactive preview',
  'Analyzing feedback impact',
  'Refining from user feedback',
  'Consolidating & refactoring',
  'Reverting last iteration',
]) {
  const adaptiveRunning = runStatus({
    ...run,
    action: 'new_job',
    result_job: '20261118-feature-1',
    job: null,
    running: true,
    progress: {
      step: 1,
      total: 3,
      label: phaseLabel,
      spec_stage: 'Pass A · Spec synthesis',
      planning_depth: 'sparse',
      gate_state: 'Intent gate · passed (assume_default)',
      experience_version: 'v0.2',
      feedback_impact: 'behavior_change · update_acceptance_criteria_and_code',
      models: ['provider/model-a'],
      active_models: ['provider/model-a'],
      agents: 1,
    },
  });
  assert(adaptiveRunning.includes(esc(phaseLabel)), `Status container should display live phase: ${phaseLabel}`);
  assert(adaptiveRunning.includes('Pass A · Spec synthesis (sparse input)'), 'Status container should display Pipeline stage');
  assert(adaptiveRunning.includes('Intent gate · passed (assume_default)'), 'Status container should display Gate status');
  assert(adaptiveRunning.includes('v0.2'), 'Status container should display Experience version');
  assert(adaptiveRunning.includes('behavior_change · update_acceptance_criteria_and_code'), 'Status container should display Feedback impact');
  assert.match(adaptiveRunning, /data-run-spec-stage/);
  assert.match(adaptiveRunning, /data-run-gate-state/);
  assert.match(adaptiveRunning, /data-run-experience-version/);
  assert.match(adaptiveRunning, /data-run-feedback-impact/);

  const adaptiveFinished = runStatus({
    ...run,
    action: 'new_job',
    result_job: '20261118-feature-1',
    job: null,
    running: false,
    exit_code: 0,
    progress: {
      step: 3,
      total: 3,
      label: phaseLabel,
      spec_stage: 'Pass B · Execution planning',
      planning_depth: 'sparse',
      gate_state: 'Plan gate · auto-passed',
      models: ['provider/model-a'],
      active_models: [],
      agents: 0,
    },
  });
  assert.match(adaptiveFinished, /Job created/, `Transient phase ${phaseLabel} must yield to completion label once done`);
  assert(adaptiveFinished.includes('Plan gate · auto-passed'));
}
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_run_status_renders_clean_title_subpoints_and_omits_models(self):
        source = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of ['runFailureGuidance', 'runDuration', 'runStatus']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.plural = (n, word, many = `${word}s`) => `${n} ${Number(n) === 1 ? word : many}`;
globalThis.act = (action) => `data-action="${action}"`;
globalThis.stoppingRuns = new Set();
globalThis.state = { disk_free_bytes: 14.0 * (1024 ** 3), disk_low: true };

const serverDone = runStatus({
  id: 'run-srv-1',
  action: 'server_start',
  title: 'Start server',
  running: false,
  exit_code: 0,
  started: 100,
  ended: 104,
  progress: { machines: ['Leemos-MBP'] },
  result: {
    headline: 'Done: OpenDAoC-Core is running.',
    details: [
      'Container opendaoc-server Started',
      'Container opendaoc-db Started',
      'Waiting for every service to be up and healthy …',
    ],
    tone: 'ok',
  },
});
assert(serverDone.includes('<strong class="run-status-stage">OpenDAoC-Core is running</strong>'), 'Server title should strip redundant Done: prefix');
assert(serverDone.indexOf('OpenDAoC-Core is running') < serverDone.indexOf('>Completed<'), 'Title should lead ahead of status pill');
assert(serverDone.includes('<li>opendaoc-server — started</li>'), 'Container subpoints should be cleanly formatted');
assert(serverDone.includes('<li>opendaoc-db — started</li>'));
assert(!serverDone.includes('Waiting for every service'), 'Stale waiting line should not appear in completed server summary');
assert(!serverDone.includes('<dt>Models</dt>'), 'Non-AI server runs should omit Models row');
assert(serverDone.includes('<dt>Machine</dt><dd data-run-machines>Leemos-MBP</dd>'));
assert(serverDone.includes('<dt>Elapsed</dt><dd data-run-elapsed>4s</dd>'));
assert(serverDone.includes('<dt>Disk space</dt><dd data-run-disk class="text-bad">14.0 GB free</dd>'));

// When disk space is healthy and the run isn't about disk space, omit the Disk space item.
globalThis.state = { disk_free_bytes: 19.0 * (1024 ** 3), disk_low: false };
const serverHealthyDisk = runStatus({
  id: 'run-srv-2',
  action: 'server_start',
  title: 'Start server',
  running: false,
  exit_code: 0,
  started: 100,
  ended: 104,
  progress: { machines: ['Leemos-MBP'] },
  result: { headline: 'Done: OpenDAoC-Core is running.', details: ['Container opendaoc-server Started'], tone: 'ok' },
});
assert(!serverHealthyDisk.includes('<dt>Disk space</dt>'), 'Healthy disk space should be omitted when not relevant to the run');
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class StoppingRunsTests(unittest.TestCase):
    """A run that started a model call in a process group of its own (as llm.py does) still stops, and stops it."""

    # The run starts a long "model call" in its own session that inherits the terminal, writes its pid, then waits.
    SCRIPT = ("import subprocess, sys, time, pathlib\n"
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
              "pathlib.Path(sys.argv[1]).write_text(str(p.pid))\n"
              "print('calling the model', flush=True)\n"
              "time.sleep(60)\n")

    def wait_until(self, check, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.1)
        return False

    def test_stop_ends_the_run_and_its_model_call(self):
        import os
        from orchestrator.web.server import PtySession

        with tempfile.TemporaryDirectory() as root:
            pid_file = Path(root) / "child.pid"
            session = PtySession("stop-test", "execute", "Build", [sys.executable, "-c", self.SCRIPT, str(pid_file)], Path(root), {})
            self.assertTrue(self.wait_until(pid_file.exists, 10))
            child = int(pid_file.read_text())
            session.stop()
            self.assertTrue(self.wait_until(lambda: not session.running, 10), "the run kept showing as stopping")

            def gone():
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    return True
                return False
            self.assertTrue(self.wait_until(gone, 5), "the model call was left running")

    def test_a_run_that_ends_by_itself_finishes_even_if_something_it_started_holds_the_terminal(self):
        from orchestrator.web.server import PtySession

        with tempfile.TemporaryDirectory() as root:
            script = ("import subprocess, sys\n"
                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'], start_new_session=True)\n"
                      "print('done', flush=True)\n")
            session = PtySession("exit-test", "execute", "Build", [sys.executable, "-c", script], Path(root), {})
            self.assertTrue(self.wait_until(lambda: not session.running, 5), "the run looked busy after it ended")
