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


class RunStatusRenderingTests(unittest.TestCase):
    def test_home_job_row_shows_only_its_running_session_and_clears_on_finish(self):
        source = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of ['jobTableRow', 'runDuration', 'runStatus', 'nextStep']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.stoppingRuns = new Set();
globalThis.ago = () => 'just now';
globalThis.jobPill = () => '<span class="pill working">Running</span>';
Date.now = () => 1000000;
const run = {id: 'r1', job: 'job-1', running: true, started: 875, title: 'Build',
  progress: {label: 'Implementing changes', task: 'Add <login>', step: 2, total: 10,
    active_models: ['provider/model-a'], models: ['provider/model-a'], agents: 1}};
globalThis.state = {runs: [{...run, id: 'other', job: 'job-2', progress: {label: 'Unrelated work'}}, run]};
const job = {id: 'job-1', title: 'Sign-in', updated: 999, kind: 'Feature', active_run: true};
let html = jobTableRow(job);
for (const part of ['Step 2 of 10', 'Implementing changes', 'provider/model-a', '1 active', '2m 5s', 'Add &lt;login&gt;']) assert(html.includes(part), part);
assert.doesNotMatch(html, /Unrelated work/);
assert.match(html, /href="#\/jobs\/job-1"/);
assert.doesNotMatch(html, /<button/);
state.runs = [{...run, job: null, result_job: 'job-1', waiting: true}];
assert.match(jobTableRow(job), /Waiting for you/);
state.runs = [{...run, stopping: true}];
assert.match(jobTableRow(job), /Stopping/);
state.runs = [{...run, running: false, exit_code: 0}];
assert.doesNotMatch(jobTableRow(job), /run-status|Step 2 of 10/);
state.runs = [];
assert.doesNotMatch(jobTableRow({...job, active_run: false}), /run-status/);
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disconnected_stream_completion_keeps_visual_check_results(self):
        source = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of ['finishRunStatus', 'nextStep']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  const prefix = name === 'finishRunStatus' ? 'async ' : '';
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = ${prefix}function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.plural = (n, noun) => `${n} ${noun}${n === 1 ? '' : 's'}`;
const panel = {}, keys = {}, input = {};
globalThis.$ = selector => ({'#next-step': panel, '#keybar': keys, '#term-input': input})[selector];
globalThis.current = {page: 'run', args: ['visual']};
globalThis.getVisualCheckForRun = async () => ({id: 'vc1', screenshots: ['screen.png']});
globalThis.runHeader = () => ({});
globalThis.setHeader = globalThis.hydrateAuthImages = globalThis.wireImagePreviews = () => {};
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
for (const name of ['runDuration', 'runStatus', 'updateRunStatus', 'nextStep']) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = function`));
}
require(require('path').join(require('path').dirname(process.argv[1]), 'html.js'));
globalThis.esc = Html.escape;
globalThis.stoppingRuns = new Set();
Date.now = () => 1000000;
const run = {id: 'r1', running: true, started: 875, action: 'execute', title: 'Build',
  progress: {label: 'Implementing changes', task: 'Add <login>', step: 2, total: 10,
    active_models: ['provider/model-a'], models: ['provider/model-a'], agents: 1}};
let html = runStatus(run);
for (const part of ['Step 2 of 10', 'Implementing changes', 'provider/model-a', '1 active', '2m 5s', 'Add &lt;login&gt;']) assert(html.includes(part), part);
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
