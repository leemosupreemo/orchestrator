"""Exercise the hosted app's real connection flow with controlled network failures."""
import json
import subprocess
import unittest
from pathlib import Path


SCRIPT = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
let now = 1000000, resets = 0, authCalls = 0, unlocked = '', failure = null, refreshed = 0;
const storage = new Map([['machine', 'mac']]);
let machine = {id: 'mac', name: 'Mac', online: true, reachable: true,
               endpoint: 'https://old.trycloudflare.com', last_seen: now / 1000};
let ticketCalls = 0;
const networkError = () => new TypeError('Load failed');
const context = {
  Map, Date: {now: () => now}, URL, AbortSignal, navigator: {onLine: scenario !== 'offline'},
  localStorage: {getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key)},
  setTimeout: (callback, ms) => { now += ms; queueMicrotask(callback); },
  Account: {MACHINE_KEY: 'machine', pendingIdea: () => null, active: () => true}, ACCOUNT_BACKEND_KEY: 'backend_account',
  stopAccountPoll() {}, showSigningIn() {}, signingIn: false, pollFailures: 2,
  state: {project: {name: 'Project'}}, getBackendUrl: () => machine.endpoint, getToken: () => 'existing',
  ConnectionLog: {add() {}},
  unlockWith: async token => { unlocked = token; },
  refreshState: async () => { refreshed++; },
  openAccount: async options => { failure = options; },
  cpApi: async (path, options) => {
    if (path === 'machine/ticket') {
      ticketCalls++;
      if (scenario === 'ticket-error') throw Object.assign(new Error('Sign-in expired'), {status: 401});
      return {ticket: 'ticket-' + ticketCalls, endpoint: machine.endpoint};
    }
    if (path === 'machines/reset-tunnel') {
      assert.equal(options.body.machine_id, 'mac'); resets++;
      return {ok: true};
    }
    assert.equal(path, 'machines');
    if (scenario === 'offline-during-lookup') context.navigator.onLine = false;
    if (scenario === 'new-address' || scenario === 'quiet' || (resets && scenario !== 'timeout' && scenario !== 'same-address')) {
      machine = {...machine, endpoint: 'https://new.trycloudflare.com', last_seen: now / 1000};
    } else if (resets && scenario === 'same-address') {
      machine = {...machine, last_seen: now / 1000};
    }
    if (scenario === 'sleeping') machine = {...machine, online: false, reachable: false};
    return {machines: [machine]};
  },
  api: async (path, options) => {
    assert.equal(path, 'auth'); authCalls++;
    assert.equal(options.body.ticket, 'ticket-' + ticketCalls);
    if (scenario === 'auth-error') throw Object.assign(new Error('Invalid ticket'), {status: 401});
    if (scenario === 'cloudflare-origin' && authCalls === 1) throw Object.assign(new Error('Origin unavailable'), {status: 530});
    if (scenario === 'gateway' && authCalls === 1) throw Object.assign(new Error('Bad Gateway'), {status: 502});
    if ((authCalls === 1 && scenario !== 'quiet') || scenario === 'quiet-fail' || scenario === 'timeout' || scenario === 'cooldown' || scenario === 'offline' || scenario === 'sleeping') throw networkError();
    return {token: 'new-token'};
  },
};
let releaseRepairLock;
if (scenario === 'locked') context.navigator.locks = {request: async (key, claim) => {
  await new Promise(resolve => {releaseRepairLock = resolve;});
  return claim();
}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('// Open one computer:'), source.indexOf('// The hosted app remembers')), context);
(async () => {
  let resetsWhileLocked = null;
  if (scenario === 'locked') {
    const opening = context.enterMachine(machine);
    await new Promise(setImmediate);
    resetsWhileLocked = resets;
    releaseRepairLock?.();
    await opening;
  } else if (scenario === 'relocate') await context.relocateMachine();
  else if (scenario.startsWith('quiet')) {
    vm.runInContext(`outageStartedAt = ${scenario === 'quiet-expired' ? now - 60000 : now - 5000}`, context);
    await context.relocateMachine();
  }
  else await context.enterMachine(machine);
  if (scenario === 'cooldown') {
    vm.runInContext('machineRepairAttempts.clear()', context); // emulate a fresh tab sharing storage
    await context.enterMachine(machine);
  }
  process.stdout.write(JSON.stringify({resets, resetsWhileLocked, authCalls, unlocked, failure, refreshed, token: storage.get('orchestrator_token') ?? '', elapsed: now - 1000000}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class ConnectionRecoveryTests(unittest.TestCase):
    def run_flow(self, scenario):
        app = Path(__file__).resolve().parents[1] / 'orchestrator/web/static/app.js'
        result = subprocess.run(['node', '-e', SCRIPT, str(app), scenario],
                                check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_failed_web_connection_repairs_once_then_opens_new_address(self):
        result = self.run_flow('repair')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['authCalls'], 2)
        self.assertEqual(result['unlocked'], 'new-token')
        self.assertIsNone(result['failure'])

    def test_new_address_is_used_without_restarting_the_tunnel(self):
        result = self.run_flow('new-address')
        self.assertEqual(result['resets'], 0)
        self.assertEqual(result['unlocked'], 'new-token')

    def test_gateway_failure_is_repaired(self):
        result = self.run_flow('gateway')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], 'new-token')

    def test_cloudflare_origin_failure_is_repaired(self):
        result = self.run_flow('cloudflare-origin')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], 'new-token')

    def test_fixed_address_reconnects_after_a_new_heartbeat(self):
        result = self.run_flow('same-address')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], 'new-token')

    def test_authentication_and_offline_failures_never_restart_the_tunnel(self):
        for scenario in ('auth-error', 'ticket-error', 'offline', 'sleeping', 'offline-during-lookup'):
            with self.subTest(scenario=scenario):
                result = self.run_flow(scenario)
                self.assertEqual(result['resets'], 0)
                self.assertEqual(result['unlocked'], '')
                self.assertIsNotNone(result['failure'])

    def test_persistent_failure_stops_waiting_and_returns_to_retry(self):
        result = self.run_flow('timeout')
        self.assertEqual(result['resets'], 1)
        self.assertIsNotNone(result['failure']['troubleshoot'])
        self.assertLessEqual(result['elapsed'], 95000)

    def test_repeated_retry_does_not_request_repeated_restarts(self):
        result = self.run_flow('cooldown')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], '')

    def test_repair_waits_for_other_tabs_before_claiming_a_restart(self):
        result = self.run_flow('locked')
        self.assertEqual(result['resetsWhileLocked'], 0)
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], 'new-token')

    def test_loaded_session_recovers_when_the_address_has_not_changed(self):
        result = self.run_flow('relocate')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], 'new-token')

    def test_a_loaded_page_reconnects_quietly_to_the_new_address(self):
        result = self.run_flow('quiet')
        self.assertEqual((result['resets'], result['authCalls'], result['refreshed']), (0, 1, 1))
        self.assertEqual(result['token'], 'new-token')
        self.assertEqual(result['unlocked'], '')  # no sign-in screen, no route reset
        self.assertIsNone(result['failure'])  # and never sent to the computers list

    def test_a_failed_quiet_attempt_stays_on_the_page_and_waits_for_the_next_poll(self):
        result = self.run_flow('quiet-fail')
        self.assertEqual((result['resets'], result['unlocked'], result['refreshed']), (0, '', 0))
        self.assertIsNone(result['failure'])

    def test_a_long_outage_falls_back_to_the_full_reconnect(self):
        result = self.run_flow('quiet-expired')
        self.assertEqual(result['resets'], 1)
        self.assertEqual(result['unlocked'], 'new-token')
