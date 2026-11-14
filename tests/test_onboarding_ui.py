"""Exercise onboarding grouping with actual page rendering and unavailable APIs."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "node is required")
class OnboardingUITests(unittest.TestCase):
    def render(self, setup=None, integrations=None, preflight=None):
        script = r'''
const fs = require('fs'), source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[2]), pages = {}, state = {you: {role: 'owner'}}, esc = s => String(s ?? '');
const ConfigurationPages = {canRunAction: () => true}, act = () => '', codeSpans = s => esc(s), $ = () => null;
let setupState;
const itemStart = source.indexOf('function setupItem(');
eval(source.slice(itemStart, source.indexOf('function setupListHtml', itemStart)));
async function api(path) {
  if(path === 'health') return {name:'App',stage:'Building',ok:0,total:1,next:'prd',items:[{id:'prd',title:'Product requirements',status:'todo',detail:'Missing',route:'#/product',label:'Add'}, {id:'release',title:'Releases',status:'todo',hint:'git tag v0.1.0 && git push --tags'}]};
  const data = {setup: input.setup, integrations: input.integrations, preflight: input.preflight};
  if(data[path]) return data[path];
  throw new Error('Unavailable');
}
const start = source.indexOf('const READINESS_CHECKS');
const end = source.indexOf('\n};', source.indexOf('pages.checkup = async', start)) + 3;
eval(source.slice(start, end));
pages.checkup().then(page => console.log(JSON.stringify(page)));
'''
        result = subprocess.run(["node", "-e", script,
                                 str(Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"),
                                 json.dumps({"setup": setup, "integrations": integrations, "preflight": preflight})],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)["html"]

    def setup(self):
        return {"items": [{"id": key, "title": key, "done": True, "required": True}
                          for key in ("project", "git", "llm", "machines")],
                "workflow_checks": []}

    def test_optional_missing_items_do_not_lower_required_count(self):
        html = self.render(self.setup(), {"integrations": [{"id": "figma", "name": "Figma", "connected": False}]}, {"items": []})
        self.assertIn("Required to run jobs", html)
        self.assertIn("4 of 4 configured", html)
        self.assertIn("Required for selected workflows", html)
        self.assertIn("Optional", html)
        self.assertIn("Figma", html)
        self.assertNotIn("<h2>Everything</h2>", html)

    def test_tools_and_environment_checks_live_in_the_onboarding_checklist(self):
        html = self.render(self.setup(), {"integrations": []}, {"items": [
            {"id": "build-tool", "title": "Xcode command line tools", "status": "fail", "detail": "Install Xcode tools", "route": "#/config/fleet", "fix": "Review machines"}
        ]})
        self.assertIn("Tools and environment", html)
        self.assertIn("Will this computer build it?", html)
        self.assertIn("Xcode command line tools", html)
        self.assertIn("Run a check", html)
        self.assertNotIn("Open Readiness", html)

    def test_release_uses_an_action_instead_of_a_shell_command(self):
        html = self.render(self.setup(), {"integrations": []}, {"items": []})
        self.assertIn("Create release tag", html)
        self.assertIn("data-create-release", html)
        self.assertNotIn("git push --tags", html)

    def test_release_dialog_cancel_local_save_and_failed_publish(self):
        script = r'''
const source = require('fs').readFileSync(process.argv[1], 'utf8'), scenario = process.argv[2];
const state = {project: {name: 'Project'}}, esc = s => String(s), posts = [], messages = [];
let routes = 0, dialogs = 0;
const route = async () => routes++, toast = text => messages.push(text);
const formDialog = async (title, html) => {
  dialogs++;
  if(dialogs === 1) {
    if(!html.includes('does not deploy')) throw new Error('Missing scope explanation');
    return scenario === 'cancel' ? null : {tag:'v0.1.0', ...(scenario === 'local' ? {} : {publish:'on'})};
  }
  return scenario === 'retry' ? {} : null;
};
const api = async (path, options) => {
  if(!options) return {commit:'a'.repeat(40),branch:'main',subject:'Release',suggested_tag:'v0.1.0',can_push:true};
  posts.push(options.body);
  return {tag:'v0.1.0',pushed:options.body.push,warning:scenario !== 'local' && posts.length === 1 ? 'Publishing failed' : ''};
};
const start = source.indexOf('async function createReleaseTag(');
eval(source.slice(start, source.indexOf('document.addEventListener', start)));
createReleaseTag().then(() => {
  const expected = scenario === 'cancel' ? 0 : scenario === 'retry' ? 2 : 1;
  if(posts.length !== expected) throw new Error('Wrong number of mutations');
  if(posts.some(p => p.commit !== 'a'.repeat(40) || p.tag !== 'v0.1.0')) throw new Error('Preview target lost');
  if(scenario === 'local' && posts[0].push) throw new Error('Local save published');
  if(scenario === 'failure' && messages.some(m => m.includes('created and published'))) throw new Error('False success');
  if(scenario !== 'cancel' && routes !== 1) throw new Error('Checklist was not refreshed');
});
'''
        for scenario in ("cancel", "local", "failure", "retry"):
            with self.subTest(scenario=scenario):
                subprocess.run(["node", "-e", script,
                                str(Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"), scenario],
                               capture_output=True, text=True, check=True)

    def test_unavailable_prerequisites_never_claim_ready(self):
        html = self.render()
        self.assertIn("Status unavailable", html)
        self.assertNotIn("Required setup is complete", html)
        self.assertIn("Tools and environment", html)

    def test_unselected_firebase_does_not_become_a_required_next_step(self):
        setup = self.setup()
        setup["items"].append({"id": "firebase", "title": "Firebase delivery", "done": False,
                               "required": False, "detail": "Send builds to testers"})
        html = self.render(setup, {"integrations": []}, {"items": []})
        self.assertNotIn("Next: Firebase", html)
        workflow = html.split("Required for selected workflows", 1)[1].split("<h2>Optional", 1)[0]
        self.assertNotIn("Firebase delivery", workflow)
        self.assertIn("Firebase delivery", html.split("<h2>Optional", 1)[1])

    def test_rejected_plugin_and_failed_build_tools_are_visible(self):
        html = self.render(self.setup(), {"integrations": [
            {"id": "figma", "name": "Figma", "connected": True, "rejected": True}]},
            {"items": [{"id": "build-tool", "title": "Build tool missing", "status": "fail", "detail": "Install npm"}]})
        self.assertIn("Build tool missing", html)
        self.assertIn("Reconnect", html)
        self.assertIn("Needs setup", html)


@unittest.skipUnless(shutil.which("node"), "node is required")
class SetupChecklistSwitchTests(unittest.TestCase):
    """DAoC Mobile's checklist said "Project configured" was not done, though it was: the answer for the project switched away from
    arrived after the switch and replaced the new project's checklist. Each answer now says which project it is for, and an
    answer that is late or for another project is ignored."""

    APP = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"

    def run_scenario(self, scenario: str):
        script = r"""
const fs = require('fs'), source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('let setupState = null;'), end = source.indexOf('function setupItem(', start);
const pending = [];
const api = (path) => new Promise((resolve) => pending.push({path, resolve}));
const state = {project: {root: '/old'}};
const make = new Function('api', 'state', 'renderSetupFab', source.slice(start, end) + '; return {loadSetup, resetSetup, get setupState() { return setupState; }};');
const page = make(api, state, () => {});
const checklist = (root, done) => ({root, items: [{id: 'project', title: 'Project configured', required: true, done}], complete: done});
const out = {};
(async () => {
""" + scenario + r"""
})().then(() => console.log(JSON.stringify(out))).catch(e => { console.error(e); process.exit(1); });
"""
        result = subprocess.run(["node", "-e", script, str(self.APP)], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_a_late_answer_for_the_old_project_does_not_replace_the_new_projects_checklist(self):
        out = self.run_scenario("""
  const first = page.loadSetup(true);                       // asked while the old project was open
  state.project = {root: '/daoc'}; page.resetSetup();       // switched
  const second = page.loadSetup(true);
  pending[1].resolve(checklist('/daoc', true)); await second;  // the new project answers first
  pending[0].resolve(checklist('/old', false)); await first;   // then the old one's slow answer arrives
  out.root = page.setupState.root; out.done = page.setupState.items[0].done;
""")
        self.assertEqual(out, {"root": "/daoc", "done": True})

    def test_an_answer_for_another_project_is_ignored_even_when_nothing_newer_was_asked(self):
        out = self.run_scenario("""
  const first = page.loadSetup(true);
  state.project = {root: '/daoc'};                           // switched without a reset
  pending[0].resolve(checklist('/old', false)); await first;
  out.state = page.setupState;
""")
        self.assertIsNone(out["state"])

    def test_the_projects_own_answer_is_used(self):
        out = self.run_scenario("""
  const first = page.loadSetup(true);
  pending[0].resolve(checklist('/old', true)); await first;
  out.root = page.setupState.root;
""")
        self.assertEqual(out["root"], "/old")

    def test_an_older_server_that_names_no_project_still_works(self):
        out = self.run_scenario("""
  const first = page.loadSetup(true);
  pending[0].resolve({items: [], complete: false}); await first;
  out.has = page.setupState !== null;
""")
        self.assertTrue(out["has"])

    def test_both_ways_of_switching_project_reset_the_checklist(self):
        source = self.APP.read_text()
        self.assertEqual(source.count("resetSetup();"), 2)  # a downloaded project opening, and switching to a local one

    def test_configure_says_when_there_was_nothing_to_change(self):
        handler = self.APP.read_text()
        handler = handler[handler.index('api("setup/configure"'):][:500]
        self.assertIn("done?.already", handler)
        self.assertIn("so nothing was changed", handler)
