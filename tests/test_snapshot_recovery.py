"""Run the actual snapshot, authentication and state-loading flow in Node."""
import json
import subprocess
import unittest
from pathlib import Path


SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const accountContext = {};
vm.createContext(accountContext);
vm.runInContext(fs.readFileSync(process.argv[1].replace(/app\.js$/, 'html.js'), 'utf8'), accountContext);
vm.runInContext(fs.readFileSync(process.argv[1].replace(/app\.js$/, 'account.js'), 'utf8'), accountContext);
let now = 1000000, interval, authCalls = 0, stateCalls = 0, snapshots = 0, routes = 0, openings = 0;
let toasts = [], releaseAuth, releaseState, releaseTicket, accountOpened = false, resets = 0;
let machine = {id: 'mac', name: 'Mac', online: true, reachable: true, has_snapshot: true,
  endpoint: 'https://mac.example.test', last_seen: 1000};
const storage = new Map([['machine', 'mac'], ['orchestrator_tunnel_repair_mac', String(now)]]);
if (scenario === 'repair') storage.delete('orchestrator_tunnel_repair_mac');
const classes = () => { const values = new Set(); return {add: x => values.add(x), remove: x => values.delete(x), contains: x => values.has(x)}; };
const app = {classList: classes()}, body = {classList: classes(), removeAttribute() {}};
const view = {innerHTML: '', onclick: null, onsubmit: null, querySelector: () => null};
const context = {
  Map, Date: {now: () => now}, URL, AbortSignal, console, navigator: {onLine: true},
  document: {body, querySelector: selector => selector === '.app' ? app : selector === '.account-card' && view.innerHTML.includes('account-card') ? {} : null},
  view, state: {project: {name: 'Cached'}, runs: [], inbox_count: 0}, signingIn: false,
  window: {firebase: {auth: () => ({currentUser: {email: 'owner@example.test'}})}},
  localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key)},
  setInterval: callback => {interval = callback; return 1;}, clearInterval: () => {interval = null;},
  setTimeout: (callback, ms) => {now += ms; queueMicrotask(callback);},
  Account: {MACHINE_KEY: 'machine', active: () => true, pendingIdea: () => null, renderSnapshot: accountContext.Account.renderSnapshot},
  ACCOUNT_BACKEND_KEY: 'backend_account', Notifications: {tabTitle: x => x, events: () => [], inboxEvents: () => [], enabled: () => false},
  ConnectionLog: {add() {}, flush() {}}, setHeader() {}, toast: (...args) => toasts.push(args),
  showSigningIn: () => {openings++; view.innerHTML = 'Opening';},
  endSigningIn: () => {context.signingIn = false;}, RESTORED_BRANCH: {clear() {}}, PUSH_KEY: 'push',
  getBackendUrl: () => storage.get('orchestrator_backend') || machine.endpoint, getToken: () => storage.get('orchestrator_token'),
  route: () => {routes++; view.innerHTML = 'Live project';}, openAccount: async () => {context.stopAccountPoll(); accountOpened = true; view.innerHTML = 'Computers';},
  lastRuns: [], lastInbox: [], consecutiveAuthFailures: 0, pollFailures: 0, outageStartedAt: 0, connectionLost: false,
  showPrdUpdate() {}, showModelsUpdate() {}, checkRunnerVersion() {}, restoreLastBranch() {}, renderSlackBtn() {}, renderPackagesBadge() {},
  ConfigurationPages: {applyRole() {}}, configurationMenu: null, $: () => null,
  renderSessionComputer() {}, renderComputerSelect() {}, renderProjectSelect() {},
  cpApi: async (path, options) => {
    if (path === 'machine/snapshot') {snapshots++; return {snapshot: {machine_id: 'mac', machine_name: 'Mac', projects: [{name: 'Cached project', root: '/project', jobs_count: 0}], recent_jobs: []}};}
    if (path === 'machines') {if (resets) machine = {...machine, last_seen: now / 1000}; return {machines: [machine]};}
    if (path === 'machines/reset-tunnel') {resets++; return {ok: true};}
    if (path === 'machine/ticket') {
      if (scenario === 'back-ticket' && options.body.machine_id === 'mac') await new Promise(resolve => {releaseTicket = resolve;});
      return {ticket: 'ticket', endpoint: options.body.machine_id === 'other' ? 'https://other.example.test' : machine.endpoint};
    }
    throw new Error('Unexpected CP call: ' + path);
  },
  api: async path => {
    if (path === 'auth') {
      authCalls++;
      if (scenario === 'delayed' || scenario === 'back-auth') await new Promise(resolve => {releaseAuth = resolve;});
      if (['persistent', 'manual'].includes(scenario) || (scenario === 'repair' && authCalls === 1)) throw new TypeError('Load failed');
      return {token: storage.get('orchestrator_backend') === 'https://other.example.test' ? 'other-token' : 'live-token'};
    }
    if (path === 'state') {
      stateCalls++;
      if (scenario === 'state-fail') throw new TypeError('Load failed');
      if (scenario === 'state-denied') throw Object.assign(new Error('Unauthorized'), {status: 401});
      if (scenario === 'delayed' || scenario === 'back-state') await new Promise(resolve => {releaseState = resolve;});
      return {project: {name: 'Live'}, runs: [], inbox: [], you: {role: 'owner'}};
    }
    throw new Error('Unexpected API call: ' + path);
  },
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function refreshState('), source.indexOf('// The hosted app\'s Computer menu')), context);
vm.runInContext(source.slice(source.indexOf('async function unlockWith('), source.indexOf('// Your computers:')), context);
vm.runInContext(source.slice(source.indexOf('// Open one computer:'), source.indexOf('// The hosted app reaches a computer')), context);
const settle = () => new Promise(setImmediate);
const tick = async ms => {now += ms; if (interval) {interval(); await settle();}};
const click = async action => {view.onclick?.({target: {closest: () => ({disabled: false, dataset: {accountAction: action, id: 'mac'}})}}); await settle();};
(async () => {
  if (scenario === 'offline') {
    machine = {...machine, online: false, reachable: false};
    await context.enterMachine(machine);
    machine = {...machine, online: true, reachable: true};
  } else if (scenario === 'online-fallback') {
    const originalApi = context.api;
    context.api = async path => {if (path === 'auth') throw new TypeError('Load failed'); return originalApi(path);};
    await context.enterMachine(machine);
  } else await context.showSnapshotScreen('mac', machine);
  if (scenario === 'manual') await click('open');
  else if (scenario !== 'online-fallback') await tick(4000);
  let during = {html: view.innerHTML, routes, toasts: [...toasts], locked: body.classList.contains('signin-gate')};
  if (scenario === 'persistent') for (let i = 0; i < 30; i++) await tick(4000);
  if (scenario === 'delayed' || scenario === 'back-auth') {
    await tick(60000); // a slow request must not overlap the next scheduled attempt
    if (scenario === 'back-auth') await click('back-machines');
    releaseAuth?.(); await settle();
    during.afterAuth = {html: view.innerHTML, routes, toasts: [...toasts]};
    releaseState?.(); await settle();
  }
  if (scenario === 'back-ticket') {
    await click('back-machines');
    await context.enterMachine({...machine, id: 'other', name: 'Other Mac', endpoint: 'https://other.example.test'});
    releaseTicket?.(); await settle();
  }
  if (scenario === 'back-state') {await click('back-machines'); releaseState?.(); await settle();}
  process.stdout.write(JSON.stringify({authCalls, stateCalls, resets, snapshots, routes, openings, toasts, during,
    html: view.innerHTML, locked: body.classList.contains('signin-gate'), signingIn: context.signingIn,
    accountOpened, backend: storage.get('orchestrator_backend'), token: storage.get('orchestrator_token'), project: context.state.project.name, remembered: storage.get('machine')}));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


class SnapshotRecoveryTests(unittest.TestCase):
    def run_flow(self, scenario):
        app = Path(__file__).resolve().parents[1] / 'orchestrator/web/static/app.js'
        result = subprocess.run(['node', '-e', SCRIPT, str(app), scenario], check=True,
                                capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_reachable_heartbeat_does_not_replace_snapshot_on_repeated_browser_failure(self):
        result = self.run_flow('persistent')
        self.assertIn('Cached project', result['html'])
        self.assertEqual((result['snapshots'], result['openings'], result['routes']), (1, 0, 0))
        self.assertEqual(result['toasts'], [])
        self.assertLessEqual(result['authCalls'], 5)
        self.assertGreaterEqual(result['authCalls'], 2)
        self.assertTrue(result['locked'])

    def test_state_failure_does_not_unlock_or_report_success(self):
        result = self.run_flow('state-fail')
        self.assertEqual(result['stateCalls'], 1)
        self.assertIn('Cached project', result['html'])
        self.assertEqual((result['routes'], result['toasts']), (0, []))
        self.assertTrue(result['locked'])

    def test_state_access_denied_keeps_read_only_snapshot(self):
        result = self.run_flow('state-denied')
        self.assertEqual((result['routes'], result['toasts']), (0, []))
        self.assertTrue(result['locked'])
        self.assertEqual(result['project'], 'Cached')

    def test_snapshot_repair_preserves_screen_and_still_opens_repaired_connection(self):
        result = self.run_flow('repair')
        self.assertEqual((result['resets'], result['authCalls'], result['stateCalls']), (1, 2, 1))
        self.assertEqual((result['openings'], result['routes']), (0, 1))

    def test_single_reconnect_keeps_snapshot_until_auth_and_state_both_succeed(self):
        result = self.run_flow('delayed')
        self.assertEqual(result['authCalls'], 1)
        self.assertIn('Cached project', result['during']['html'])
        self.assertIn('Cached project', result['during']['afterAuth']['html'])
        self.assertEqual(result['during']['afterAuth']['toasts'], [])
        self.assertEqual((result['routes'], result['stateCalls']), (1, 1))
        self.assertEqual(len(result['toasts']), 1)
        self.assertFalse(result['locked'])

    def test_back_cancels_pending_auth_and_state_opening(self):
        for scenario in ('back-auth', 'back-state'):
            with self.subTest(scenario=scenario):
                result = self.run_flow(scenario)
                self.assertTrue(result['accountOpened'])
                self.assertEqual(result['html'], 'Computers')
                self.assertEqual(result['project'], 'Cached')
                self.assertEqual((result['routes'], result['toasts']), (0, []))

    def test_late_ticket_cannot_overwrite_another_computer_after_back(self):
        result = self.run_flow('back-ticket')
        self.assertEqual(result['remembered'], 'other')
        self.assertEqual(result['backend'], 'https://other.example.test')
        self.assertEqual(result['token'], 'other-token')
        self.assertEqual(result['routes'], 1)

    def test_offline_machine_still_recovers_automatically_when_browser_can_connect(self):
        result = self.run_flow('offline')
        self.assertEqual((result['routes'], result['stateCalls']), (1, 1))
        self.assertEqual(result['remembered'], 'mac')
        self.assertFalse(result['locked'])

    def test_manual_retry_failure_keeps_snapshot_and_back_button(self):
        result = self.run_flow('manual')
        self.assertIn('Cached project', result['html'])
        self.assertEqual((result['snapshots'], result['openings'], result['routes']), (1, 0, 0))

    def test_online_machine_fallback_does_not_claim_mac_is_offline(self):
        result = self.run_flow('online-fallback')
        self.assertIn('Cached project', result['html'])
        self.assertFalse(any('is offline' in message[0] for message in result['toasts']))
        self.assertTrue(any('connection' in message[0].lower() for message in result['toasts']))
        self.assertNotIn('Offline snapshot', result['html'])
