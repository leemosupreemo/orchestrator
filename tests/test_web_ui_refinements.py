"""Behavior checks for the web UI's job and reference flows."""
import json
import subprocess
import unittest
from pathlib import Path


class WebUIRefinementTests(unittest.TestCase):
    def run_js(self, functions, scenario):
        script = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
for (const name of JSON.parse(process.argv[2])) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, `Missing function ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  eval(source.slice(start, end).replace(`function ${name}`, `globalThis.${name} = function`));
}
""" + scenario
        result = subprocess.run(
            ["node", "-e", script,
             str(Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"),
             json.dumps(functions)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_coverage_advisor_passes_real_gaps_and_review_choice_to_the_planner(self):
        self.run_js(["coverageJobParams"], """
const inventory = { coverage: { overall_coverage_pct: 42, metric: 'lines', timestamp: '2026-10-01' } };
const cases = { cases: [{ id: 'TC-1', title: 'Rejoin', area: 'Lobby', status: 'unassigned' },
  { id: 'TC-2', title: 'Login', area: 'Auth', status: 'covered' }] };
const job = coverageJobParams(inventory, cases, { target: '75', execution: 'review' });
assert.equal(job.type, 'coverage');
assert.equal(job.branch_mode, 'current');
assert.equal(job.no_dispatch, true);
assert.equal(job.yolo, false);
assert.match(job.spec, /42%/);
assert.match(job.spec, /75%/);
assert.match(job.spec, /TC-1.*Rejoin/);
assert.doesNotMatch(job.spec, /TC-2/);
assert.match(job.spec, /prioriti[sz]e/i);
assert.match(job.spec, /measure/i);
const automatic = coverageJobParams(inventory, cases, { target: '', execution: 'automatic' });
assert.equal(automatic.no_dispatch, false);
assert.doesNotMatch(automatic.spec, /NaN/);
""")

    def test_coverage_advisor_never_treats_estimates_as_measured_or_accepts_bad_targets(self):
        self.run_js(["coverageJobParams"], """
const job = coverageJobParams({ coverage: { overall_coverage_pct: 90, estimated: true } }, { cases: [] }, {});
assert.doesNotMatch(job.spec, /90%/);
assert.match(job.spec, /not measured/i);
for (const target of ['0', '101', 'oops', 'Infinity']) {
  assert.throws(() => coverageJobParams({}, { cases: [] }, { target }));
}
""")

    def test_feature_brief_retains_prefill_and_structures_optional_answers(self):
        self.run_js(["featureSpec"], """
assert.equal(featureSpec({ details: 'Keep the existing behavior' }), 'Keep the existing behavior');
const spec = featureSpec({ details: 'Imported ticket', audience: 'Players who disconnected',
  outcome: 'Return to their seat', acceptance: 'Seat preserved\\nNo duplicate player', constraints: 'No new dependencies' });
for (const answer of ['Imported ticket', 'Players who disconnected', 'Return to their seat',
  'Seat preserved\\nNo duplicate player', 'No new dependencies']) assert(spec.includes(answer));
assert.equal(featureSpec({}), '');
""")

    def test_git_and_secondary_pages_have_parents_for_direct_links(self):
        self.run_js(["routeParent"], """
for (const page of ['git', 'help', 'readiness', 'checkup', 'ux-review', 'devlogs']) {
  assert(routeParent({page, args: []}), page);
}
assert.equal(routeParent({page: 'home', args: []}), null);
assert.equal(routeParent({page: 'tests', args: []}), null);
assert.equal(routeParent({page: 'docs', args: ['product']}), '#/product');
""")

    def test_connected_reference_submit_adds_pending_text_and_blocks_empty_selection(self):
        self.run_js(["wireReferenceSubmit"], """
const listeners = {};
let links = '[]', added = 0, prevented = 0;
const input = { value: 'https://figma.com/design/example', focus: () => {} };
const picker = { querySelector: selector => selector === '.lp-input' ? input
  : selector === '.lp-add' ? {click: () => { added++; links = '[{"provider":"figma","ref":"example"}]'; input.value = ''; }}
  : {get value() { return links; }} };
const button = {};
const form = { addEventListener: (name, fn) => { listeners[name] = fn; }, removeEventListener: (name) => { delete listeners[name]; } };
const dialog = { addEventListener: (name, fn) => { listeners[name] = fn; } };
const elements = {'#dialog-form': form, '#dialog': dialog, '#dialog-ok': button, '#dialog-body .link-picker': picker};
globalThis.$ = selector => elements[selector];
globalThis.toast = () => {};
wireReferenceSubmit();
listeners.submit({submitter: button, preventDefault: () => { prevented++; }});
assert.equal(added, 1);
assert.equal(prevented, 0);
links = '[]';
listeners.submit({submitter: button, preventDefault: () => { prevented++; }});
assert.equal(prevented, 1);
listeners.close();
assert.equal(listeners.submit, undefined);
""")
