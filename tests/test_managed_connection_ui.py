"""Execute account rendering and the actual account-screen handlers in Node."""
import json
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'orchestrator/web/static'
SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
require(process.argv[1] + '/html.js'); require(process.argv[1] + '/account.js');
const A = globalThis.Account;
const machine = {id: 'mac', name: '<Mac>', online: true, reachable: false, api_version: 4,
  connection: {state: 'failed', detail: '<img src=x onerror=evil()>'}};
const render = extra => A.renderMachines([{...machine, ...extra}], {back: 'Project', now: 100});
if (process.argv[2] === 'render') {
  const safe = 'https://login.tailscale.com/f/funnel?node=nABC-123';
  const unsafe = ['javascript:alert(1)', 'https://login.tailscale.com.evil.test/f/funnel?node=a',
    'https://u:p@login.tailscale.com/f/funnel?node=a', 'https://login.tailscale.com:443/f/funnel?node=a',
    'https://login.tailscale.com/f/funnel?node=a&evil=1', 'https://login.tailscale.com/f/funnel?node=a#evil',
    'https://login.tailscale.com/f/funnel?node=a\nevil', 'https://login.tailscale.com/f/funnel?node=a\n', 'https://login.tailscale.com/f/funnel?node='];
  console.log(JSON.stringify({
    required: A.REQUIRED_RUNNER_API, states: Object.fromEntries(['checking','repairing','needs_setup','connected','failed'].map(state =>
      [state, render({connection: {state}})])),
    browserFailure: A.renderMachines([{...machine, reachable: true, connection: {state: 'connected'}}], {troubleshoot: {machine, error: 'Load failed'}}),
    pending: render({connection_command_pending: true}), offline: render({online: false}),
    legacy: render({api_version: 3, connection: undefined}), escaped: render({}),
    setup: render({connection: {state: 'needs_setup', setup_url: safe}}),
    safe: typeof A.safeFunnelSetupURL === 'function' ? A.safeFunnelSetupURL(safe) : null,
    unsafe: unsafe.map(url => render({connection: {state: 'needs_setup', setup_url: url}})),
  }));
} else {
  const calls = [], renders = [], timers = [], opened = [];
  const view = {contains: () => false, querySelectorAll: () => [],
    set innerHTML(value) {renders.push(value);}, get innerHTML() {return renders.at(-1);}};
  const context = {Account: A, view, accountScreenGeneration: 0, state: {project: {name: 'Keep me'}, inbox_count: 0},
    stopAccountPoll() {}, signingIn: false, setHeader() {}, Notifications: {tabTitle: x => x},
    document: {title: '', activeElement: null, body: {classList: {add() {}}, removeAttribute() {}},
      querySelector: q => q === '.account-card' ? {} : {classList: {add() {}}}},
    $: () => null, window: {}, getToken: () => 'KEEP-TOKEN', getBackendUrl: () => 'KEEP-BACKEND',
    fetch: async () => ({ok: false}), setInterval: callback => {timers.push(callback); return 1;},
    location: {hash: '#/computers'}, localStorage: {getItem: () => 'mac'},
    cpApi: async (path, options) => {calls.push({path, options}); return {machines: [{...machine,
      reachable: true, connection: {state: 'connected'}}]};},
    toast() {}, openAccount: async options => {opened.push(options);}, enterMachine: async () => opened.push('entered'),
  };
  vm.createContext(context);
  const source = fs.readFileSync(process.argv[1] + '/app.js', 'utf8');
  vm.runInContext(source.slice(source.indexOf('function showAccountScreen('), source.indexOf('// The token is shown once')), context);
  (async () => {
    const action = process.argv[2];
    context.showAccountScreen([machine], '', null, {chosen: true});
    const button = {disabled: false, dataset: {accountAction: action, id: 'mac', name: '<Mac>'}};
    await view.onclick({target: {closest: () => button}});
    const afterClick = view.innerHTML;
    await timers[0]();
    console.log(JSON.stringify({calls, afterClick, afterPoll: view.innerHTML, opened, project: context.state.project.name}));
  })().catch(error => {console.error(error); process.exitCode = 1;});
}
"""


class ManagedConnectionUITests(unittest.TestCase):
    def run_ui(self, scenario='render'):
        result = subprocess.run(['node', '-e', SCRIPT, str(STATIC), scenario],
                                check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_actual_cards_show_connection_progress_and_machine_presence_separately(self):
        result = self.run_ui()
        self.assertEqual(result['required'], 4)
        for state, label in [('checking', 'Checking'), ('repairing', 'Repairing'), ('needs_setup', 'Needs setup'),
                             ('connected', 'Connected'), ('failed', 'Connection unavailable')]:
            with self.subTest(state=state):
                self.assertIn(label, result['states'][state])
                self.assertIn('Computer online', result['states'][state])
        self.assertIn('Repair requested', result['pending'])
        self.assertIn('disabled', result['pending'])
        self.assertIn('data-account-action="repair-connection"', result['states']['failed'])
        self.assertIn('data-account-action="setup-funnel"', result['states']['failed'])
        self.assertIn('Offline', result['offline'])
        self.assertIn('this browser couldn’t reach its web connection', result['browserFailure'])
        self.assertIn('Connection unavailable', result['browserFailure'])
        self.assertNotIn('<img', result['escaped'])
        self.assertIn('&lt;img', result['escaped'])
        self.assertIn('data-account-action="reset-tunnel"', result['legacy'])
        self.assertNotIn('data-account-action="repair-connection"', result['legacy'])

    def test_setup_uses_only_exact_owner_url_and_explains_automatic_completion(self):
        result = self.run_ui()
        self.assertEqual(result['safe'], 'https://login.tailscale.com/f/funnel?node=nABC-123')
        self.assertIn('Enable Funnel', result['setup'])
        self.assertIn('automatically', result['setup'])
        self.assertIn('account owner', result['setup'])
        self.assertIn('https://tailscale.com/download', result['states']['needs_setup'])
        self.assertIn('sign in', result['states']['needs_setup'])
        for html in result['unsafe']:
            self.assertNotIn('Enable Funnel', html)
            self.assertNotIn('href="javascript:', html)
            self.assertNotIn('href="https://u:p@', html)

    def test_fix_and_setup_clicks_use_control_plane_and_keep_project_and_progress(self):
        for action, command in [('repair-connection', 'repair'), ('setup-funnel', 'setup_funnel')]:
            with self.subTest(action=action):
                result = self.run_ui(action)
                self.assertEqual(result['calls'][0]['path'], 'machines/repair-connection')
                self.assertEqual(result['calls'][0]['options'], {'method': 'POST', 'body': {'machine_id': 'mac', 'action': command}})
                self.assertIn('Repair requested', result['afterClick'])
                self.assertIn('Connected', result['afterPoll'])
                self.assertEqual(result['opened'], [])
                self.assertEqual(result['project'], 'Keep me')

    def test_legacy_reconnect_still_calls_legacy_route(self):
        result = self.run_ui('reset-tunnel')
        self.assertEqual(result['calls'][0]['path'], 'machines/reset-tunnel')
