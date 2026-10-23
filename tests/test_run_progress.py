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
globalThis.ago = () => 'just now';
globalThis.jobPill = () => '<span class="pill working">Running</span>';
globalThis.jobKind = (j) => j.kind;
globalThis.jiraChip = () => '';
Date.now = () => 1000000;
const run = {id: 'r1', job: 'job-1', running: true, started: 875, title: 'Build', idle: 0,
  progress: {label: 'Implementing changes', task: 'Add <login>', step: 2, total: 10}};
globalThis.state = {runs: [run]};
const job = {id: 'job-1', title: 'Sign-in', updated: 999, kind: 'Feature', active_run: true};
const row = jobTableRow(job);
assert.doesNotMatch(row, /job-tests/);
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
let html = runStatus({...run, summary: 'Improve test coverage toward 80%', progress: {...run.progress, machines: ['mac-mini'], suites: ['tests/a.py', 'tests/b.py'], suite: 2}});
for (const part of ['Improve test coverage toward 80%', 'mac-mini (active)', '2 planned · working on 2 of 2: b.py']) assert(html.includes(part), part);
assert.match(runStatus(run), /Assigning a machine/);
html = runStatus(run);
for (const part of ['Step 2 of 10', 'Implementing changes', 'provider/model-a', '(working)', '2m 5s', 'Add &lt;login&gt;', 'run-status-pct', '10%']) assert(html.includes(part), part);
const slow = runStatus({...run, progress: {label: 'Waiting for claude-opus-4-7', agents: 1, models: ['gpt-5.4', 'claude-opus-4-7'], active_models: ['claude-opus-4-7'],
  calls: [{model: 'claude-opus-4-7', age: 485, timeout: 1200}],
  failed: [{model: 'gpt-5.4', reason: "Not supported <here>"}, {model: 'o1', reason: "couldn't start (is it installed?)"}]}});
for (const part of ['Waiting for claude-opus-4-7', 'claude-opus-4-7 (working for 8m)', '2 models couldn&#39;t run', 'Not supported &lt;here&gt;', 'No answer from claude-opus-4-7 yet (8m)', 'after 20 minutes it gives up', 'The run is still going']) assert(slow.includes(part) || slow.includes(part.replace('&#39;', "'")), part);
const fresh = runStatus({...run, progress: {label: 'Waiting for m', agents: 1, models: ['m'], active_models: ['m'], calls: [{model: 'm', age: 30, timeout: 600}], failed: []}});
assert(fresh.includes('m (working for 30s)'));
assert.doesNotMatch(fresh, /No answer from|Skipped models/);  // nothing to say until it has been quiet a while, or something was skipped
assert.doesNotMatch(runStatus({...run, running: false, exit_code: 0, progress: {...run.progress, calls: [{model: 'm', age: 500, timeout: 600}]}}), /No answer from/);
const flagged = runStatus({...run, running: false, exit_code: 0, progress: {label: 'Reading the model\'s answer', models: ['gpt-5.4', 'claude-opus-4-7'], failed: [{model: 'gpt-5.4', reason: 'Not supported'}]},
  result: {headline: '4 test files added with 49 tests, not checked yet', details: ['Location: Tests/'], tone: 'warn', partial: true}});
for (const part of ['Partially complete', 'Paused: the work is saved',  '4 test files added with 49 tests', 'Location: Tests/', 'claude-opus-4-7']) assert(flagged.includes(part), part);
assert.doesNotMatch(flagged, /Reading the model|<dd>gpt-5.4/);  // not where it was, and not the model that couldn't run
assert.match(runStatus({...run, running: false, exit_code: 0, progress: {label: 'Scheduling worker'}}), /Job execution complete|Run complete/);  // a finished run is never headlined with where it started
assert.match(runStatus({...run, waiting: true}), /Waiting for you/);
assert.match(runStatus({...run, stopping: true}), /Stopping/);
const planning = runStatus({...run, progress: {label: 'Planning implementation', agents: 0, models: [], active_models: []}});
assert.match(planning, /Planning implementation/);
assert.doesNotMatch(planning, /Step .* of|undefined|NaN/);
assert.match(runStatus({...run, running: false, exit_code: 1}), /banner failed/);
assert.match(runStatus({...run, running: false, exit_code: 0}), /banner done/);
assert.match(runStatus({...run, running: false, stopped: true}), /Run was stopped/);
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
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
