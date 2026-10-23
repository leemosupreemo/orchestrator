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
    def render(self, report=None, runs=None, settings=None, latest=None, captures=None, has_interface=None, saved=None):
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
  if (path === 'ux-pass') return {captures: input.captures || [], passes: input.latest ? [input.latest, {id: 'older', at: input.report.at, counts: input.report.counts}] : input.report ? [{id: 'latest', completed_at: input.report.completed_at}] : [], settings: input.settings || {}, saved: input.saved || {}, browser: true, has_interface: input.has_interface};
  if (path === 'ux-pass/latest') return input.latest || input.report;
  if (path === 'ux-pass/older') return input.report;
  throw new Error(`Unexpected request ${path}`);
}
const helpers = source.slice(source.indexOf('const UX_SEVERITY'), source.indexOf('pages["ux-review"] = async'));
const pageStart = source.indexOf('pages["ux-review"] = async');
const page = source.slice(pageStart, source.indexOf('\n};', pageStart) + 3);
eval(helpers + '\n' + page + '\nglobalThis.uxCheckIds = Object.keys(UX_CHECKS);');
pages['ux-review']([], new URLSearchParams(input.latest ? 'id=older' : '')).then(result => console.log(JSON.stringify({...result, check_ids: globalThis.uxCheckIds})));
'''
        result = subprocess.run(["node", "-e", script, str(STATIC / "app.js"),
                                 json.dumps({"report": report, "runs": runs, "settings": settings, "latest": latest, "captures": captures, "has_interface": has_interface, "saved": saved}),
                                 str(STATIC / "html.js")], text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_empty_review_explains_purpose_and_shows_checks_before_start(self):
        page = self.render()
        self.assertIn("Find usability and design issues. Choose what to fix.", page["html"])
        self.assertIn("Not started", page["html"])
        self.assertIn("Visibility of system status", page["html"])
        self.assertIn("Keyboard focus", page["html"])
        self.assertIn("Not checked", page["html"])
        self.assertIn("Usability heuristics", page["html"])
        self.assertIn("Not set up: reviewing code only", page["html"])

    def test_page_reads_in_the_order_people_use_it(self):
        report = {**self.report(), "findings": [{"area": "ux", "severity": 3, "screen": "Home", "problem": "Two primary buttons", "fix": "Make one secondary"}],
                  "counts": {"major": 1, "findings": 1, "passed": 1, "failed": 0}, "fix_text": "Fix it"}
        page = self.render(report=report, captures=[], saved={"url": "http://localhost:3000"}, settings={"url": "http://localhost:3000"},
                           latest={**report, "id": "latest", "completed_at": 1790845300}, runs=[])
        html = page["html"]
        order = [html.index(marker) for marker in ('id="ux-current-progress"', 'id="ux-setup"', 'id="ux-results"', "Two primary buttons",
                                                  'id="ux-screenshot-gallery"', 'id="ux-checklist"', "Earlier reviews")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(html.count('data-action="ux_pass"'), 0)  # one Run button, in the header
        self.assertIn("Create a job to fix the main ones", html)

    def test_setup_is_a_closed_one_line_summary_once_screens_are_set_up(self):
        page = self.render(saved={"url": "http://localhost:3000", "android": {"package": "com.example.app"}},
                           settings={"url": "http://localhost:3000"})
        html = page["html"]
        self.assertIn("Web http://localhost:3000 · Android com.example.app", html)
        self.assertNotIn('<details class="fold" open>', html[html.index('id="ux-setup"'):html.index('id="ux-setup"') + 100])
        for key, shown in (("web", True), ("android", True), ("ios", False), ("mac", False)):
            tag = f'data-platform="{key}"'
            self.assertEqual("hidden" not in html[html.index(tag):html.index(tag) + 60], shown, key)
        self.assertIn('name="p_web" checked', html)
        self.assertNotIn('name="p_ios" checked', html)

    def test_setup_opens_first_when_nothing_is_set_up(self):
        html = self.render()["html"]
        self.assertIn('<details class="fold" open>', html[html.index('id="ux-setup"'):])
        self.assertIn("What does your app run on?", html)
        self.assertNotIn(" checked>", html[html.index('class="ux-platforms"'):html.index('data-platform="web"')])  # you pick; nothing is assumed

    def test_a_project_with_no_interface_says_so_before_a_review_is_run(self):
        page = self.render(settings={"url": "", "simulator": False}, has_interface=False)
        self.assertIn("No interface files or screens found yet", page["html"])
        self.assertIn("disabled", page["actions"])  # nothing to run yet
        ready = self.render(settings={"url": "", "simulator": False}, has_interface=True)
        self.assertNotIn("No interface files or screens found yet", ready["html"])
        self.assertIn("reviewing code only", ready["html"])
        self.assertNotIn("disabled", ready["actions"])

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

    def test_a_review_of_an_empty_project_is_marked_as_a_caution_not_a_success(self):
        report = {**self.report(), "coverage": "empty", "summary": "Nothing to review."}
        page = self.render(report=report)
        self.assertIn('<span class="pill attention">Nothing to review</span>', page["html"])
        self.assertIn("no interface files or screens yet", page["html"])
        self.assertNotIn("Review complete", page["html"])
        self.assertNotIn("checklist items pass", page["html"])

    def test_a_review_without_screenshots_says_only_the_code_was_read(self):
        page = self.render(report={**self.report(), "coverage": "code_only"})
        self.assertIn('<span class="pill attention">Code only</span>', page["html"])
        self.assertIn("No screenshots were captured, so only the interface code was reviewed", page["html"])

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
eval(source.slice(start, source.indexOf('pages["ux-review"] = async', start)));
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
