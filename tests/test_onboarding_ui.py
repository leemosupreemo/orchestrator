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
let setupState;
const itemStart = source.indexOf('function setupItem(');
eval(source.slice(itemStart, source.indexOf('function setupListHtml', itemStart)));
async function api(path) {
  if(path === 'health') return {name:'App',stage:'Building',ok:0,total:1,next:'prd',items:[{id:'prd',title:'Product requirements',status:'todo',detail:'Missing',route:'#/product',label:'Add'}, {id:'release',title:'Releases',status:'todo',hint:'git tag v0.1.0 && git push --tags'}]};
  const data = {setup: input.setup, integrations: input.integrations, preflight: input.preflight};
  if(data[path]) return data[path];
  throw new Error('Unavailable');
}
const start = source.indexOf('pages.checkup = async');
eval(source.slice(start, source.indexOf('\n};',start)+3));
pages.checkup().then(page => console.log(JSON.stringify(page)));
'''
        result = subprocess.run(["node", "-e", script,
                                 str(Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"),
                                 json.dumps({"setup": setup, "integrations": integrations, "preflight": preflight})],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)["html"]

    def setup(self):
        return {"items": [{"id": key, "title": key, "done": True, "required": True}
                          for key in ("project", "git", "llm", "machines", "models")],
                "workflow_checks": []}

    def test_optional_missing_items_do_not_lower_required_count(self):
        html = self.render(self.setup(), {"integrations": [{"id": "figma", "name": "Figma", "connected": False}]}, {"items": []})
        self.assertIn("Required to run jobs", html)
        self.assertIn("5 of 5 configured", html)
        self.assertIn("Required for selected workflows", html)
        self.assertIn("Optional", html)
        self.assertIn("Figma", html)
        self.assertNotIn("<h2>Everything</h2>", html)

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
        self.assertIn('href="#/readiness"', html)

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

