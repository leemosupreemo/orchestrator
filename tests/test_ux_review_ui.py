"""Render the UX review page with controlled run and report states."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from orchestrator import ux_review

STATIC = Path(__file__).resolve().parents[1] / "orchestrator/web/static"


@unittest.skipUnless(shutil.which("node"), "node is required for UI rendering")
class UxReviewUITests(unittest.TestCase):
    def render(self, report=None, runs=None, settings=None, latest=None, captures=None):
        script = r'''
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[2]);
require(process.argv[3]);
const esc = Html.escape, pages = {}, state = {runs: input.runs || []};
const act = action => `data-action="${action}"`;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const ago = () => 'a moment ago';
const clamped = text => esc(text);
async function api(path) {
  if (path === 'ux-pass') return {captures: input.captures || [], passes: input.latest ? [input.latest, {id: 'older', at: input.report.at, counts: input.report.counts}] : input.report ? [{id: 'latest', completed_at: input.report.completed_at}] : [], settings: input.settings || {}, saved: {}, browser: true};
  if (path === 'ux-pass/latest') return input.latest || input.report;
  if (path === 'ux-pass/older') return input.report;
  throw new Error(`Unexpected request ${path}`);
}
const helpers = source.slice(source.indexOf('const UX_SEVERITY'), source.indexOf('// What jobs need'));
const pageStart = source.indexOf('pages["ux-review"] = async');
const page = source.slice(pageStart, source.indexOf('\n};', pageStart) + 3);
eval(helpers + '\n' + page + '\nglobalThis.uxCheckIds = Object.keys(UX_CHECKS);');
pages['ux-review']([], new URLSearchParams(input.latest ? 'id=older' : '')).then(result => console.log(JSON.stringify({...result, check_ids: globalThis.uxCheckIds})));
'''
        result = subprocess.run(["node", "-e", script, str(STATIC / "app.js"),
                                 json.dumps({"report": report, "runs": runs, "settings": settings, "latest": latest, "captures": captures}),
                                 str(STATIC / "html.js")], text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_empty_review_explains_purpose_and_shows_checks_before_start(self):
        page = self.render()
        self.assertIn("Improve your app", page["html"])
        self.assertIn("Find usability and design issues. Choose what to fix.", page["html"])
        self.assertIn("Not started", page["html"])
        self.assertIn("Visibility of system status", page["html"])
        self.assertIn("Keyboard focus", page["html"])
        self.assertIn("Not checked", page["html"])
        self.assertIn("Usability heuristics", page["html"])
        self.assertIn("Code only", page["html"])

    def test_job_screens_are_visible_before_the_first_product_review(self):
        capture = {"kind": "job", "id": "job-1", "title": "New settings", "at": "2026-10-09T12:00:00Z", "limits": [],
                   "screens": [{"file": "settings.png", "route": "/settings", "width": 390, "dark": False}]}
        page = self.render(captures=[capture])
        self.assertIn("UI screenshots", page["html"])
        self.assertIn("ux-screens/job/job-1/settings.png", page["html"])
        self.assertIn("New settings", page["html"])
        self.assertIn("data-preview-auth", page["html"])

    def test_running_review_shows_actual_stage_and_log_link(self):
        page = self.render(runs=[{"id": "run-1", "action": "ux_pass", "running": True,
                                 "started": 1, "last_line": "Reviewing screenshots", "progress": {
                                     "step": 3, "total": 4, "label": "Check visual design"}}])
        self.assertIn("Step 3 of 4", page["html"])
        self.assertIn("Check visual design", page["html"])
        self.assertIn('href="#/runs/run-1"', page["html"])
        self.assertIn("Reviewing screenshots", page["html"])
        self.assertIn("disabled", page["actions"])

    def test_successful_run_without_new_report_keeps_old_results_labelled_previous(self):
        page = self.render(report=self.report(), runs=[{"id": "new", "action": "ux_pass", "running": False,
                                                       "started": 2000000000, "exit_code": 0}])
        self.assertIn("Results unavailable", page["html"])
        self.assertIn("Previous results", page["html"])
        self.assertNotIn("Review complete", page["html"])

    def test_old_selected_report_does_not_hide_a_new_completed_review(self):
        latest = {**self.report(), "id": "latest", "completed_at": 2000000010}
        page = self.render(report=self.report(), latest=latest, runs=[{
            "id": "new", "action": "ux_pass", "running": False, "started": 2000000000, "exit_code": 0}])
        self.assertIn("Review complete", page["html"])
        self.assertIn("Previous results", page["html"])
        self.assertNotIn("Results unavailable", page["html"])

    def test_completion_uses_server_epoch_instead_of_browser_local_date(self):
        report = {**self.report(), "at": "2099-01-01T00:00:00", "completed_at": 1500000000}
        page = self.render(report=report, runs=[{
            "id": "new", "action": "ux_pass", "running": False, "started": 1600000000, "exit_code": 1}])
        self.assertIn("Review failed", page["html"])
        self.assertIn("Previous results", page["html"])

    def test_visible_check_catalog_covers_all_runner_checks(self):
        page = self.render()
        self.assertEqual(set(page["check_ids"]), ux_review.CHECK_IDS)

    def test_failed_run_does_not_show_an_old_report_as_new_success(self):
        report = self.report()
        page = self.render(report=report, runs=[{"id": "failed", "action": "ux_pass", "running": False,
                                               "started": 2000000000, "exit_code": 1, "last_line": "Capture failed"}])
        self.assertIn("Review failed", page["html"])
        self.assertIn("Previous results", page["html"])
        self.assertIn("Capture failed", page["html"])

    def test_dirty_screen_settings_keep_the_form_while_progress_updates(self):
        script = r"""
const source = require('fs').readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function updateUxReviewWhileEditing(');
eval(source.slice(start, source.indexOf('// What jobs need', start)));
const form = {dataset: {dirty: 'true'}, url: 'http://unsaved.example', contains: () => false};
let replacements = 0, headerUpdates = 0;
const replacement = {querySelector: () => ({append: () => {}})};
const $ = selector => selector === '#ux-settings' ? form : {replaceWith: element => {
  if (element !== replacement) throw new Error('Wrong progress replacement');
  replacements++;
}};
const document = {activeElement: {}, createElement: () => ({querySelector: () => replacement})};
const setHeader = () => {headerUpdates++;};
if (!updateUxReviewWhileEditing({html: '<section>New progress</section>'})) throw new Error('Dirty form not protected');
if (form.url !== 'http://unsaved.example' || replacements !== 1 || headerUpdates !== 1) throw new Error('Progress refresh lost settings');
form.dataset.dirty = 'false';
if (updateUxReviewWhileEditing({html: ''})) throw new Error('Clean form blocked report refresh');
"""
        subprocess.run(["node", "-e", script, str(STATIC / "app.js")], check=True, capture_output=True, text=True)

    def report(self):
        return {"id": "latest", "at": "2026-10-01T09:00:00", "completed_at": 1790845200, "counts": {"major": 0, "findings": 0, "passed": 1, "failed": 0},
                "findings": [], "screens": [], "summary": "Code review complete", "limits": "No screenshots",
                "checklist": [{"id": "ux.status", "status": "pass", "note": "Loading state is visible"},
                              {"id": "ux.forms", "status": "n/a", "note": "No forms on these screens"},
                              {"id": "design.focus", "status": "n/a", "note": "not reported"}]}

    def test_report_exposes_pass_not_applicable_and_unreported_checks(self):
        page = self.render(report=self.report())
        self.assertIn("Visibility of system status", page["html"])
        self.assertIn("Loading state is visible", page["html"])
        self.assertIn("Not applicable", page["html"])
        self.assertIn("No forms on these screens", page["html"])
        self.assertIn("Not checked", page["html"])
        self.assertIn("Keyboard focus", page["html"])
        self.assertNotIn('<details class="fold"><summary class="card-h"><h2>Usability heuristics', page["html"])
