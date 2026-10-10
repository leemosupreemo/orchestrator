import subprocess
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class SetupRefreshUiTests(unittest.TestCase):
    def run_refresh_scenario(self, scenario):
        app = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
let rendered = null, release, completed = 0;
const listeners = {};
const context = vm.createContext({
  Date, document: {
    hidden: false, activeElement: null,
    querySelector: () => null,
    addEventListener: (name, handler) => { listeners[name] = handler; },
  },
  window: { addEventListener: () => {} },
  $: () => ({ open: false }),
  view: { contains: () => false },
  current: { page: 'home', args: [], query: {}, rendered: '' },
  LIVE: new Set(['home']), state: { runs: [], project: { root: '/project' } },
  refreshState: async () => {}, renderSetupFab: () => {}, route: () => {},
  api: async () => {
    if (process.argv[2] === 'pending') await new Promise(resolve => { release = resolve; });
    return { complete: false, seen: false, required_done: completed };
  },
  pages: { home: async () => ({ html: vm.runInContext('setupState.required_done', context) }) },
  signatureOf: result => String(result.html),
  apply: result => { rendered = result.html; },
});
vm.runInContext(source.slice(source.indexOf('let setupState ='), source.indexOf('function setupItem(')), context);
vm.runInContext(source.slice(source.indexOf('async function tick()'), source.indexOf('const connBtn =')), context);
(async () => {
  if (process.argv[2] === 'pending') {
    const pending = vm.runInContext('tick()', context);
    await new Promise(setImmediate);
    assert.equal(rendered, null, 'must wait for fresh checklist before rendering');
    completed = 1;
    release();
    await pending;
    assert.equal(rendered, 1, 'completed step appears in the same refresh');
  } else {
    await vm.runInContext('loadSetup(true)', context);
    await vm.runInContext('tick()', context);
    assert.equal(rendered, 0);
    completed = 1;
    if (process.argv[2] === 'typing') {
      context.document.activeElement = { matches: () => true };
      context.view.contains = () => true;
    }
    context.document.hidden = true;
    listeners.visibilitychange?.();
    await new Promise(setImmediate);
    assert.equal(rendered, 0, 'hidden tab does not refresh');
    context.document.hidden = false;
    listeners.visibilitychange?.();
    await new Promise(setImmediate);
    if (process.argv[2] === 'typing') {
      assert.equal(rendered, 0, 'refresh must not interrupt typing');
      context.document.activeElement = null;
      context.view.contains = () => false;
      await vm.runInContext('tick()', context);
    }
    assert.equal(rendered, 1, 'returning to tab refreshes even inside cache lifetime');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script, str(app), scenario], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_poll_waits_for_updated_checklist(self):
        self.run_refresh_scenario("pending")

    def test_returning_to_tab_refreshes_checklist(self):
        self.run_refresh_scenario("visible")

    def test_refresh_does_not_interrupt_typing(self):
        self.run_refresh_scenario("typing")


class SetupPanelUiTests(unittest.TestCase):
    def run_panel_scenario(self, close_during_refresh):
        app = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const panel = { hidden: true, innerHTML: '' };
let finishRefresh;
const clicks = [];
const context = vm.createContext({
  $: () => panel,
  setupState: { required_done: 1 },
  document: { addEventListener: (name, handler) => { clicks.push(handler); } },
  loadSetup: () => new Promise(resolve => { finishRefresh = () => {
    context.setupState = { required_done: 2 }; resolve();
  }; }),
  renderSetupPanel: () => { panel.innerHTML = String(context.setupState.required_done); },
  toast: () => {},
});
vm.runInContext(source.slice(source.indexOf('async function openSetupPanel()'), source.indexOf('pages.home =')), context);
const tap = () => clicks.forEach(click => click({ target: { closest: selector => selector.includes('#setup-fab') } }));
(async () => {
  tap();
  assert.equal(panel.hidden, false, 'first tap opens panel without waiting for server');
  assert.equal(panel.innerHTML, '1', 'cached checklist is immediately available');
  if (process.argv[2] === 'close') {
    tap();
    assert.equal(panel.hidden, true, 'second tap closes panel during refresh');
  }
  finishRefresh();
  await new Promise(setImmediate);
  if (process.argv[2] === 'close') {
    assert.equal(panel.hidden, true, 'late server response must not reopen panel');
  } else {
    assert.equal(panel.innerHTML, '2', 'open panel receives refreshed checklist');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        scenario = "close" if close_during_refresh else "open"
        result = subprocess.run(["node", "-e", script, str(app), scenario], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_tap_opens_cached_checklist_then_refreshes(self):
        self.run_panel_scenario(False)

    def test_closing_during_refresh_keeps_panel_closed(self):
        self.run_panel_scenario(True)


class SetupDismissUiTests(unittest.TestCase):
    def test_skip_dismisses_setup_before_save_finishes(self):
        app = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const handlers = {};
let rendered, navigated = false, finishSave;
const context = vm.createContext({
  setupState: { required_done: 1, required_total: 7, seen: false },
  state: { project: { root: '/project' } },
  setupDismissedRoots: new Set(),
  $: selector => ({ addEventListener: (name, handler) => { handlers[selector] = handler; } }),
  setupListHtml: () => '',
  api: () => new Promise(resolve => { finishSave = resolve; }),
  apply: result => { rendered = result; },
  route: () => { navigated = true; },
  toast: () => {},
});
vm.runInContext(source.slice(source.indexOf('function setupPage('), source.indexOf('function renderSetupFab(')), context);
vm.runInContext('setupPage(setupState).after()', context);
handlers['#setup-skip']();
assert.equal(context.setupState.seen, true, 'skip takes effect before server save');
assert.equal(navigated, true, 'Home navigation starts immediately');
assert.equal(rendered.title, 'Home', 'setup screen is dismissed while Home loads');
finishSave();
"""
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refresh_during_save_preserves_dismissal(self):
        app = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
let skip;
const context = vm.createContext({
  state: { project: { root: '/project' } }, Date,
  $: () => ({ addEventListener: (name, handler) => { skip = handler; } }),
  setupListHtml: () => '', renderSetupFab: () => {}, apply: () => {}, route: () => {},
  api: path => path === 'config/setup-seen' ? new Promise(() => {}) : Promise.resolve({seen: false, complete: false}),
});
vm.runInContext(source.slice(source.indexOf('let setupState ='), source.indexOf('function setupItem(')), context);
vm.runInContext(source.slice(source.indexOf('function setupPage('), source.indexOf('function renderSetupFab(')), context);
(async () => {
  await vm.runInContext('loadSetup(true)', context);
  vm.runInContext('setupPage({...setupState, required_done: 1, required_total: 7}).after()', context);
  skip();
  await vm.runInContext('loadSetup(true)', context);
  assert.equal(vm.runInContext('setupState.seen', context), true, 'pending save must not let refresh reopen setup');
  context.state.project.root = '/another-project';
  await vm.runInContext('loadSetup(true)', context);
  assert.equal(vm.runInContext('setupState.seen', context), false, 'another project retains its own setup state');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
