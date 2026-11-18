"""Exercise final review rendering and action payloads without starting real jobs."""

from pathlib import Path
import shutil
import subprocess
import unittest


STATIC = Path(__file__).resolve().parents[1] / "orchestrator/web/static"


@unittest.skipUnless(shutil.which("node"), "node is required")
class FinalJobReviewUITests(unittest.TestCase):
    def node(self, scenario):
        script = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
require(process.argv[2]);
const esc = Html.escape;
const attrJSON = o => esc(JSON.stringify(o));
const act = (action, params) => `data-action="${action}" data-params='${attrJSON(params)}'`;
const state = {actions: {merge: {confirm: 'Merge?'}, complete: {confirm: 'Complete?'}}};
const calls = [], shown = [], lookups = [];
let detail, values = {reviewed: 'on'};
const ok = {disabled: false};
const $ = selector => selector === '#dialog-ok' ? ok : null;
async function api(path) { lookups.push(path); return detail; }
async function formDialog(title, html, label) {
  shown.push({title, html, label});
  return values;
}
async function runAction(action, params, options) { calls.push({action, params, options}); }
for (const name of ['jobReviewCanApprove', 'jobReviewDeliveryHtml', 'jobFinalReviewHtml', 'reviewJobDialog']) {
  const match = source.match(new RegExp('(?:async )?function ' + name + '\\([^]*?\\n\\}'));
  assert(match, 'Missing final review behavior: ' + name);
  eval(match[0]);
}
const dialogSource = source.slice(source.indexOf('const dialogs = {'), source.indexOf('\nconst stoppingRuns'));
eval(dialogSource.replace('const dialogs =', 'globalThis.dialogs ='));
function review(overrides = {}) {
  return {ready: true, can_approve: true, reason: 'Ready for your review.', revision: 'current-sha', approval: null,
    delivery: {status: 'unavailable', required: false, reason: 'No app delivery configured.', url: null, version: null, build: null, built_at: null}, ...overrides};
}
(async () => {
""" + scenario + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", script, str(STATIC / "app.js"), str(STATIC / "html.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_delivery_allows_review_and_empty_state_stays_closed(self):
        self.node(r"""
const html = jobFinalReviewHtml({id: 'job-1', review: review(), next: {action: 'complete', label: 'Complete'}});
assert.match(html, /id="finalize-section"/);
assert.match(html, /No app delivery configured/);
assert.match(html, /data-action="complete"/);
assert.doesNotMatch(html, /disabled/);
const empty = jobFinalReviewHtml({id: 'job-1'});
assert.doesNotMatch(empty, /data-action="(?:merge|complete)"/);
assert.match(empty, /review.*unavailable/i);
""")

    def test_unsuccessful_delivery_blocks_approval_and_offers_retry(self):
        self.node(r"""
for (const status of ['pending', 'failed', 'stale']) {
  const delivery = {status, required: true, reason: 'Needs a current build.', url: 'https://old.example.test'};
  const html = jobFinalReviewHtml({id: 'job-1', review: review({can_approve: false, delivery}), next: {action: 'merge'}});
  assert.match(html, /Needs a current build/);
  assert.match(html, /data-action="deliver"/);
  assert.match(html, /href="#\/delivery"/);
  assert.match(html, /disabled/);
  assert.doesNotMatch(html, /href="https:\/\/old/);
}
""")

    def test_delivery_evidence_escapes_fields_and_rejects_unsafe_links(self):
        self.node(r"""
const delivery = {status: 'delivered', required: true, reason: 'Ready <img>',
  url: 'https://preview.example.test/?a="x"&b=2', version: '<script>', build: '4"2', built_at: 'now <b>'};
const render = d => jobFinalReviewHtml({id: 'job-1', review: review({delivery: d}), isComplete: true});
const html = render(delivery);
assert.match(html, /href="https:\/\/preview.example.test\/\?a=&quot;x&quot;&amp;b=2"/);
assert.match(html, /rel="noopener noreferrer"/);
assert.match(html, /&lt;script&gt;/);
assert.match(html, /4&quot;2/);
assert.match(html, /now &lt;b&gt;/);
assert.doesNotMatch(html, /<script>|<img>|data-action="(?:merge|complete)"/);
for (const url of ['javascript:alert(1)', 'data:text/html,hi', '//evil.example', 'https://safe.test/\" onclick=\"bad']) {
  const unsafe = render({...delivery, url});
  assert.doesNotMatch(unsafe, /href="(?:javascript:|data:|\/\/)|onclick="bad/);
}
""")

    def test_dialog_uses_fresh_state_and_requires_explicit_acknowledgment(self):
        self.node(r"""
for (const action of ['merge', 'complete']) {
  detail = {job: {title: 'Work <b>'}, summary: {id: 'job/1'}, final_review: review({revision: action + '-fresh'})};
  values = {};
  await dialogs[action]({job: 'job/1', review_revision: 'old', reviewed: true});
  assert.equal(calls.length, 0);
  assert.match(shown.at(-1).html, /type="checkbox"[^>]*name="reviewed"[^>]*required/);
  assert.match(shown.at(-1).html, /Work &lt;b&gt;/);
  values = {reviewed: 'on'};
  await dialogs[action]({job: 'job/1', review_revision: 'old'});
  assert.deepEqual(calls.pop(), {action, params: {job: 'job/1', reviewed: true, review_revision: action + '-fresh'}, options: {skipConfirm: true}});
}
assert.deepEqual(lookups, Array(4).fill('jobs/job%2F1'));
""")

    def test_dialog_cannot_approve_blocked_or_missing_review(self):
        self.node(r"""
for (const final_review of [null, review({ready: false, can_approve: false}),
    review({can_approve: false, delivery: {status: 'failed', required: true, reason: 'Build failed.'}})]) {
  detail = {job: {title: 'A job'}, final_review};
  await dialogs.merge({job: 'job-1'});
  assert.equal(calls.length, 0);
  assert.doesNotMatch(shown.at(-1).html, /name="reviewed"/);
}
""")

    def test_direct_final_actions_open_review_instead_of_starting_a_run(self):
        self.node(r"""
const match = source.match(/async function runAction\([^]*?\n\}/);
assert(match);
const realRunAction = eval('(' + match[0] + ')');
for (const action of ['merge', 'complete']) {
  detail = {job: {title: 'A job'}, final_review: review()};
  values = null;
  await realRunAction(action, {job: 'job-1'});
}
assert.equal(calls.length, 0);
assert.equal(shown.length, 2);
assert.deepEqual(lookups, ['jobs/job-1', 'jobs/job-1']);
""")

    def test_real_job_page_renders_early_final_and_completed_jobs(self):
        self.node(r"""
const vm = require('vm');
const page = source.match(/pages\.job = async[^]*?\n\};/)[0];
const context = {pages: {}, api: async path => path === 'visual-checks' ? {checks: []} : detail,
  state: {project: {}}, matchMedia: () => ({matches: false}), SIDE_RUNS: new Set(), stoppingRuns: new Set(),
  esc, act, jobFinalReviewHtml, URL, runningMark: '', PROVIDER_LABEL: {},
  plural: (n, label) => `${n} ${label}`, Markdown: {render: esc}};
// Unrelated cards are display-only boundaries; the real page and review renderer run below.
for (const name of ['formatJobTests', 'featureChip', 'jobChangeSummary', 'jobHeaderActions', 'jobContextRail',
  'jobRunsCard', 'jobDocsOutlineHtml', 'uxReviewCard', 'clamped']) context[name] = () => '';
vm.createContext(context);
vm.runInContext(page, context);
for (const fixture of [
    {status: 'planned', tasks: ['One task'], final_review: review({ready: false, can_approve: false})},
    {status: 'review-needed', tasks: [], final_review: review()},
    {status: 'completed', tasks: [], final_review: review({approval: {reviewed_by: 'A reviewer', reviewed_at: 'today'}})}]) {
  detail = {summary: {id: 'job-1', title: 'Work', status: fixture.status,
    state: {group: 'needs_you', tone: 'attention', label: 'Review', next: fixture.status === 'planned' ? {action: 'approve', label: 'Approve plan'} : fixture.status === 'completed' ? null : {action: 'complete', label: 'Complete'}}},
    job: {type: 'feature', tasks: fixture.tasks}, tasks: fixture.tasks, completed_tasks: [],
    outputs: [], runs: [], docs: [], changes: {files: ['app.js']}, links: [], final_review: fixture.final_review};
  const rendered = await context.pages.job(['job-1']);
  assert.match(rendered.html, /id="finalize-section"/);
  assert.match(rendered.html, /Review &amp; approve/);
  if (fixture.status === 'completed') {
    assert.match(rendered.html, /A reviewer/);
    assert.doesNotMatch(rendered.html, />Merged</);
  }
}
""")
