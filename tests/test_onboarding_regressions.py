"""Behavior regressions for the November onboarding changes; no vendor installs or accounts required."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import ai_setup, sandbox, sentry_device
from orchestrator.scripts import ai_connect, model_registry
from orchestrator.web import server

ROOT = Path(__file__).resolve().parents[1]


class IsolatedHomeTests(unittest.TestCase):
    def test_reset_only_removes_the_named_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            first = sandbox.prepare('first', base=base)
            second = sandbox.prepare('second', base=base)
            (first / 'settings').write_text('old')
            (second / 'settings').write_text('keep')
            sandbox.prepare('first', base=base, reset=True)
            self.assertEqual(list(first.iterdir()), [])
            self.assertEqual((second / 'settings').read_text(), 'keep')
            sandbox.wipe('first', base=base)
            self.assertEqual((second / 'settings').read_text(), 'keep')

    def test_invalid_names_cannot_create_or_delete_outside_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 'sandboxes'
            sentinel = Path(d) / 'keep'
            sentinel.write_text('keep')
            for name in ('', '..', '../keep', '/tmp/escape', 'a/b', 'a' * 65):
                for operation in (sandbox.prepare, sandbox.wipe):
                    with self.subTest(name=name, operation=operation.__name__):
                        with self.assertRaises(ValueError):
                            operation(name, base=base)
                        self.assertEqual(sentinel.read_text(), 'keep')

    def test_a_child_process_gets_no_host_credentials_even_with_keep_tools(self):
        with tempfile.TemporaryDirectory() as d:
            home = sandbox.prepare(base=Path(d))
            source = {'PATH': '/usr/bin:/bin', 'HOME': '/host', 'OPENAI_API_KEY': 'private',
                      'GH_TOKEN': 'private', 'ANTHROPIC_API_KEY': 'private',
                      'ORCHESTRATOR_USER_STATE_DIR': '/host/state', 'PYTHONPATH': '/host/code'}
            for keep in (False, True):
                with self.subTest(keep_tools=keep):
                    result = subprocess.run([sys.executable, '-c',
                        'import os,json,pathlib; print(json.dumps(dict(os.environ))); pathlib.Path.home().joinpath("child").write_text("ok")'],
                        env=sandbox.environment(home, keep_tools=keep, base_env=source),
                        capture_output=True, text=True, check=True, timeout=10)
                    env = json.loads(result.stdout)
                    self.assertEqual(env['HOME'], str(home))
                    for key in ('OPENAI_API_KEY', 'GH_TOKEN', 'ANTHROPIC_API_KEY', 'ORCHESTRATOR_USER_STATE_DIR'):
                        self.assertNotIn(key, env)
                    self.assertEqual((home / 'child').read_text(), 'ok')
                    self.assertEqual(env['PYTHONPATH'], str(ROOT))

    def test_sample_project_is_repeatable_and_its_tests_actually_pass(self):
        with tempfile.TemporaryDirectory() as d:
            home = sandbox.prepare(base=Path(d))
            project = sandbox.sample_project(home)
            (project / 'personal-work').write_text('keep')
            self.assertEqual(sandbox.sample_project(home), project)
            self.assertEqual((project / 'personal-work').read_text(), 'keep')
            result = subprocess.run([sys.executable, '-m', 'unittest', 'discover'], cwd=project,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Ran 1 test', result.stderr)


class RealConnectProcessTests(unittest.TestCase):
    def test_each_provider_runs_its_sign_in_and_refreshes_only_after_readiness(self):
        # A real executable records argv and creates the sign-in marker: skipping sign-in cannot satisfy readiness.
        for provider, cli, args in [('claude', 'claude', ['auth', 'login']), ('codex', 'codex', ['login']),
                                    ('pool', 'pool', ['login']), ('agy', 'agy', [])]:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as d:
                folder = Path(d)
                tool = folder / cli
                marker = folder / 'signed-in'
                tool.write_text(f'#!{sys.executable}\nimport sys,json,pathlib\npathlib.Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))\n')
                tool.chmod(0o755)
                messages, refreshed = [], []
                rc = ai_connect.connect(provider, which=lambda name: str(tool) if name == cli else None,
                    run=lambda argv: subprocess.call(argv), ready=lambda _: marker.exists(), say=messages.append,
                    after=lambda: refreshed.append(marker.read_text()))
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(marker.read_text()), args)
                self.assertEqual([json.loads(value) for value in refreshed], [args])
                self.assertIn('is connected', ' '.join(messages))

    def test_failed_installer_never_runs_sign_in_or_refreshes_models(self):
        commands, refreshed, messages = [], [], []
        def fail(argv):
            commands.append(argv)
            return 7
        rc = ai_connect.connect('pool', which=lambda name: '/bin/' + name if name in ('curl', 'bash') else None,
            run=fail, ready=lambda _: True, say=messages.append, after=lambda: refreshed.append(True))
        self.assertEqual(rc, 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(refreshed, [])
        self.assertIn("didn't install", ' '.join(messages))

    def test_installer_success_without_an_executable_is_not_reported_as_connected(self):
        commands, refreshed = [], []
        rc = ai_connect.connect('pool', which=lambda name: '/bin/' + name if name in ('curl', 'bash') else None,
            run=lambda argv: commands.append(argv) or 0, ready=lambda _: True, say=lambda _: None,
            after=lambda: refreshed.append(True))
        self.assertEqual(rc, 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(refreshed, [])

    def test_failed_sign_in_never_enables_models(self):
        refreshed = []
        rc = ai_connect.connect('codex', which=lambda _: shutil.which('false'), run=lambda argv: subprocess.call(argv),
            ready=lambda _: False, say=lambda _: None, after=lambda: refreshed.append(True))
        self.assertEqual(rc, 1)
        self.assertEqual(refreshed, [])

    def test_untrusted_provider_values_are_rejected_before_execution(self):
        for provider in ('', '../../tool', 'codex; echo bad', 'gemini'):
            with self.subTest(provider=provider), self.assertRaises(server.UIError):
                server.build_ai_connect({'provider': provider})


class FreeAiFailureTests(unittest.TestCase):
    def test_model_list_failures_do_not_enable_any_models(self):
        for error in (RuntimeError('offline'), OSError('missing'), subprocess.TimeoutExpired('opencode', 90)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as d:
                refreshed = []
                def unavailable(_):
                    raise error
                result = ai_setup.ensure_free_ai(Path(d), which=lambda _: '/fake/opencode',
                    list_models=unavailable, refresh_known=lambda: None, refresh=refreshed.append,
                    known=['opencode/example-free'])
                self.assertFalse(result['ok'])
                self.assertEqual(result['step'], 'verify')
                self.assertEqual(result['fallback']['id'], 'pool')
                self.assertEqual(refreshed, [])

    def test_only_offered_known_free_models_are_enabled_when_catalog_refresh_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            refreshed = []
            def offline():
                raise OSError('offline')
            result = ai_setup.ensure_free_ai(root, which=lambda _: '/fake/opencode',
                list_models=lambda _: ['opencode/present-free', 'opencode/paid', 'opencode/new-free'],
                known=['opencode/absent-free', 'opencode/paid', 'opencode/present-free'],
                refresh_known=offline, refresh=refreshed.append)
            self.assertTrue(result['ok'])
            self.assertEqual(result['models'], ['opencode/present-free'])
            self.assertEqual(refreshed, [root])

    def test_real_model_listing_reports_exit_failure_instead_of_using_partial_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / 'opencode'
            tool.write_text('#!/bin/sh\necho opencode/example-free\necho offline >&2\nexit 1\n')
            tool.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                ai_setup._list_models(str(tool))

    def test_cached_provider_offer_excludes_paid_and_removed_models(self):
        # The cache file is real; don't replace model_usable or its cache reader.
        with tempfile.TemporaryDirectory() as d, patch.object(model_registry, 'provider_cache_path', return_value=Path(d) / 'models.json'):
            path = model_registry.provider_cache_path()
            path.write_text(json.dumps({'opencode': ['opencode/present-free']}))
            for name, expected in [('opencode/present-free', True), ('opencode/paid', False), ('opencode/removed-free', False)]:
                with self.subTest(model=name):
                    model = model_registry.ModelMetadata(id=name, family='opencode', tier=model_registry.ModelTier.LOW,
                        capabilities=[], cost_factor=0, required_clis=['opencode'])
                    self.assertEqual(model_registry.model_usable(model), expected)


class SuiteHistoryPersistenceTests(unittest.TestCase):
    def test_finished_run_survives_reload_without_altering_other_suites_or_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            suites = [{'name': 'A', 'tests': 2}, {'name': 'B', 'tests': 3}]
            server.record_test_run(root, 'A', at=10)
            self.assertIsNone(server.with_last_runs(root, suites)[0]['last_run']['ok'])
            server.record_test_run(root, 'A', at=10, ok=False)
            server.record_test_run(root, 'B', at=20, ok=True)
            rows = server.with_last_runs(root, suites)
            self.assertEqual(rows[0]['last_run'], {'at': 10, 'ok': False, 'all': False})
            self.assertEqual(rows[1]['last_run'], {'at': 20, 'ok': True, 'all': False})
            self.assertEqual(suites, [{'name': 'A', 'tests': 2}, {'name': 'B', 'tests': 3}])


class SentryInvalidResponseTests(unittest.TestCase):
    def test_malformed_start_never_returns_a_usable_session(self):
        for status, payload in [(503, {}), (200, {}), (200, {'device_code': 'secret'}), (200, {'user_code': 'public'})]:
            with self.subTest(status=status, payload=payload), self.assertRaises(sentry_device.DeviceError):
                sentry_device.start('client', post=lambda *_: (status, payload))

    def test_missing_token_is_not_reported_as_success_or_exposed_in_errors(self):
        for status, payload in [(200, {}), (503, {}), (400, {'error': 'invalid_client'})]:
            with self.subTest(status=status):
                result = sentry_device.poll('client', 'private-device-code', post=lambda *_: (status, payload))
                self.assertEqual(result['status'], 'error')
                self.assertNotIn('private-device-code', json.dumps(result))
                self.assertNotIn('token', result)

    def test_malformed_organization_is_refused(self):
        for payload in (None, {}, [], ['acme'], [{'name': 'Acme'}], [{'slug': ''}]):
            with self.subTest(payload=payload), self.assertRaises(sentry_device.DeviceError):
                sentry_device.organization('secret-token', get=lambda *_: payload)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for JavaScript behavior tests')
class OnboardingJavaScriptTests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run(['node', '-e', body, str(ROOT / 'orchestrator/web/static')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def reset_flow(self, body):
        # Run the actual app handler block with controlled DOM boundaries and clock; no source-text assertions.
        return self.run_js(r'''
const fs = require('fs'), path = require('path');
const dir = process.argv[1];
const nodes = {};
function $(id) { return nodes[id] ||= {hidden: true, value: '', disabled: false, isConnected: true,
  textContent: '', innerHTML: '', focus() {}, addEventListener() {}, querySelector() { return null; }}; }
const emailPanel = $('#email-signin-panel'), emailInput = $('#email-input'), emailForgotBtn = $('#forgot');
const calls = [], modes = [], notices = [];
let tick = null, failure = null;
const auth = {sendPasswordResetEmail: async email => {calls.push(email); if(failure) throw {code: failure};}};
const firebase = {auth: () => auth}, window = {firebase};
const PasswordReset = require(path.join(dir, 'reset-password.js'));
const esc = s => s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const setMode = (...args) => modes.push(args), toast = (...args) => notices.push(args);
const setInterval = fn => {tick = fn; return 1;}, clearInterval = () => {tick = null;};
const source = fs.readFileSync(path.join(dir,'app.js'),'utf8');
const start = source.indexOf('  const resetStep = $("#email-reset-step")');
const end = source.indexOf('  if (emailLinkBtn && emailInput)', start);
if (start < 0 || end < 0) throw Error('Reset handler block not found');
(async () => {
 await eval('(async () => {' + source.slice(start,end) + '\n' + process.env.UNUSED_TEST_BODY + '\n})()');
})().catch(e => {console.error(e);process.exit(1)});
'''.replace("process.env.UNUSED_TEST_BODY", json.dumps(body)))

    def test_password_reset_opens_empty_and_preserves_address_on_return(self):
        out = self.reset_flow('''
openPasswordReset();
const opened = !resetStep.hidden && !resetForm.hidden && $('#email-auth-form').hidden;
resetEmail.value = ' ana@example.com ';
closePasswordReset();
console.log(JSON.stringify({opened, resetHidden: resetStep.hidden, email: emailInput.value, modes, calls}));
''')
        self.assertTrue(out['opened'])
        self.assertTrue(out['resetHidden'])
        self.assertEqual(out['email'], 'ana@example.com')
        self.assertEqual(out['modes'], [['signin', 'ana@example.com']])
        self.assertEqual(out['calls'], [])

    def test_password_reset_success_displays_neutral_answer_and_delays_resend(self):
        out = self.reset_flow('''
openPasswordReset('ana@example.com');
failure = 'auth/user-not-found';
await sendReset();
const before = {disabled: resetAgain.disabled, hidden: resetForm.hidden, message: resetSent.innerHTML};
for (let i=0; i<29; i++) tick();
const at29 = resetAgain.disabled;
tick();
console.log(JSON.stringify({before, at29, after: resetAgain.disabled, tickStopped: tick === null, calls}));
''')
        self.assertTrue(out['before']['disabled'])
        self.assertTrue(out['before']['hidden'])
        self.assertIn('If an account exists', out['before']['message'])
        self.assertTrue(out['at29'])
        self.assertFalse(out['after'])
        self.assertTrue(out['tickStopped'])
        self.assertEqual(out['calls'], ['ana@example.com'])

    def test_password_reset_network_failure_allows_retry_then_back_clears_timer(self):
        out = self.reset_flow('''
openPasswordReset('ana@example.com');
failure = 'auth/network-request-failed';
await sendReset();
const failed = {formVisible: !resetForm.hidden, canSubmit: !resetSubmit.disabled, error: resetSent.textContent};
failure = null;
await sendReset();
closePasswordReset();
console.log(JSON.stringify({failed, calls, tickStopped: tick === null}));
''')
        self.assertTrue(out['failed']['formVisible'])
        self.assertTrue(out['failed']['canSubmit'])
        self.assertIn('connection', out['failed']['error'])
        self.assertEqual(out['calls'], ['ana@example.com', 'ana@example.com'])
        self.assertTrue(out['tickStopped'])

    def test_ai_page_only_offers_connections_to_owner_and_never_to_ready_tools(self):
        out = self.run_js(r'''
const path = require('path'), dir = process.argv[1];
require(path.join(dir,'html.js')); require(path.join(dir,'configuration.js'));
const providers = [
 {id:'claude',name:'Claude',installed:false,ready:false,can_connect:true},
 {id:'codex',name:'Codex',installed:true,ready:false,can_connect:true},
 {id:'pool',name:'Poolside',installed:true,ready:true,can_connect:true},
 {id:'opencode',name:'OpenCode',installed:false,ready:false}];
const render = role => globalThis.ConfigurationPages.render('ai', {viewer:{role},keys:[],ai:{providers}}).html;
console.log(JSON.stringify({owner:render('owner'),member:render('member')}));
''')
        from html.parser import HTMLParser
        class Buttons(HTMLParser):
            def __init__(self, html):
                super().__init__(); self.attrs = []; self.feed(html)
            def handle_starttag(self, tag, attrs):
                if tag == 'button': self.attrs.append(dict(attrs))
        owner, member = Buttons(out['owner']).attrs, Buttons(out['member']).attrs
        self.assertEqual([b['data-ai-connect'] for b in owner if 'data-ai-connect' in b], ['codex', 'claude'])
        self.assertEqual(sum('data-free-ai' in b for b in owner), 1)
        self.assertFalse(any('data-ai-connect' in b or 'data-free-ai' in b for b in member))


if __name__ == '__main__':
    unittest.main()
