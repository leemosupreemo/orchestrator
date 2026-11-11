"""Source-backed Home status, cached suggestions and prefilled job links."""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import home_status
from tests.test_web_ui import ServerTestCase, UI_HEADERS

STATIC = Path(__file__).resolve().parents[1] / 'orchestrator/web/static'


class SuggestionTests(unittest.TestCase):
    def test_revision_ignores_job_order_and_tracks_requirements_and_job_progress(self):
        jobs = [{'id': 'a', 'status': 'planned', 'updated': '1'}, {'id': 'b', 'status': 'completed', 'updated': '2'}]
        first = home_status.revision('A snake game', jobs)
        self.assertEqual(first, home_status.revision('A snake game', jobs[::-1]))
        self.assertNotEqual(first, home_status.revision('A chess game', jobs))
        self.assertNotEqual(first, home_status.revision('A snake game', [dict(jobs[0], status='completed'), jobs[1]]))

    def test_three_fallbacks_are_available_before_a_model_answers(self):
        with tempfile.TemporaryDirectory() as d:
            result = home_status.view(Path(d), '', [])
            self.assertTrue(result['stale'])
            self.assertEqual(len(result['suggestions']), 3)
            self.assertTrue(all(s['summary'] and s['spec'] for s in result['suggestions']))

    def test_model_result_is_saved_and_reused_until_inputs_change(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            answer = json.dumps({'suggestions': [dict(title=t, summary=t + ' for the snake game', type='feature', spec='Keep the scope small.')
                                                 for t in ('Add a score display', 'Add pause controls', 'Add game over')]})
            result = home_status.generate(root, 'A snake game', [], lambda _: answer)
            self.assertEqual(result['source'], 'ai')
            self.assertFalse(result['stale'])
            self.assertEqual(home_status.view(root, 'A snake game', []), result)
            self.assertTrue(home_status.view(root, 'New requirements', [])['stale'])
            self.assertEqual(result['suggestions'][0]['title'], 'Add a score display')

    def test_bad_model_results_and_offline_models_keep_three_suggestions_without_retrying(self):
        def offline(_):
            raise RuntimeError('private provider diagnostic')
        for ask in (offline, lambda _: 'not json', lambda _: '{"suggestions": []}',
                    lambda _: json.dumps({'suggestions': [{'title': 'same', 'summary': 'same', 'spec': 'same', 'type': 'feature'}] * 3})):
            with self.subTest(ask=ask), tempfile.TemporaryDirectory() as d:
                result = home_status.generate(Path(d), 'A snake game', [], ask)
                self.assertEqual(len(result['suggestions']), 3)
                self.assertFalse(result['stale'])
                self.assertEqual(result['source'], 'project')
                self.assertNotIn('private provider diagnostic', json.dumps(result))

    def test_full_disk_keeps_suggestions_in_memory_without_repeating_the_model_call(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('orchestrator.home_status.os.replace', side_effect=OSError('No space left on device')):
                calls = []
                def offline(prompt):
                    calls.append(prompt)
                    raise RuntimeError('offline')
                first = home_status.generate(Path(d), 'A game', [], offline)
                second = home_status.generate(Path(d), 'A game', [], offline)
            self.assertEqual(first, second)
            self.assertFalse(second['stale'])
            self.assertEqual(len(calls), 1)

    def test_unknown_job_types_cannot_be_returned_by_the_model(self):
        with self.assertRaises(ValueError):
            home_status.parse_reply(json.dumps({'suggestions': [dict(title=str(i), summary=str(i), spec='x', type='shell') for i in range(3)]}))


class HomeStatusApiTests(ServerTestCase):
    def test_get_returns_suggestions_without_calling_a_model(self):
        with patch('orchestrator.web.server.UIHandler._model_call', side_effect=AssertionError('GET invoked a model')):
            res, result = self.request('GET', '/api/home-status')
        self.assertEqual(res.status, 200)
        self.assertEqual(len(result['suggestions']), 3)

    def test_generation_reuses_the_cache_instead_of_spending_again_on_reload(self):
        with patch('orchestrator.web.server.UIHandler._model_call', side_effect=RuntimeError('offline')) as ask:
            res, result = self.request('POST', '/api/home-status/suggestions', body={}, headers=UI_HEADERS)
            self.assertEqual(res.status, 202)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, view = self.request('GET', '/api/home-status')
                if not view['stale']:
                    break
                time.sleep(.02)
            self.assertFalse(view['stale'])
            _, again = self.request('POST', '/api/home-status/suggestions', body={}, headers=UI_HEADERS)
            self.assertIsNone(again['task'])
            self.assertEqual(ask.call_count, 1)

    def test_member_cannot_trigger_model_generation(self):
        from tests.test_web_ui import SignInTests
        token = SignInTests.sign_in(self, source="settings")
        with patch('orchestrator.web.server.UIHandler._model_call', side_effect=AssertionError('member invoked model')):
            res, _ = SignInTests.as_user(self, token, 'POST', '/api/home-status/suggestions', body={},
                                        sources={'tester@example.com': 'settings'})
        self.assertEqual(res.status, 403)

    def test_concurrent_page_visits_share_one_background_call(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def answer(*args, **kwargs):
            entered.set()
            release.wait(5)
            raise RuntimeError('offline')
        with patch('orchestrator.web.server.UIHandler._model_call', side_effect=answer) as ask:
            try:
                _, first = self.request('POST', '/api/home-status/suggestions', body={}, headers=UI_HEADERS)
                self.assertTrue(entered.wait(2))
                _, second = self.request('POST', '/api/home-status/suggestions', body={}, headers=UI_HEADERS)
                self.assertEqual(first['task'], second['task'])
                self.assertEqual(ask.call_count, 1)
            finally:
                release.set()
                for _ in range(100):
                    if self.server.tasks.get(first['task'])['status'] != 'running':
                        break
                    time.sleep(.02)

    def test_late_edits_in_long_requirements_invalidate_suggestions(self):
        from orchestrator import prd
        path = self.root / prd.PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Product requirements\n\n## Core features\n" + "A" * 6000 + " old requirement\n")
        _, first = self.request('GET', '/api/home-status')
        shared = prd.document_path(self.root)
        shared.write_text(shared.read_text().replace('old requirement', 'new requirement'))
        _, second = self.request('GET', '/api/home-status')
        self.assertNotEqual(first['revision'], second['revision'])

    def test_unauthenticated_request_is_denied(self):
        res, _ = self.request('POST', '/api/home-status/suggestions', body={}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 401)


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class HomeStatusRenderTests(unittest.TestCase):
    def render(self, data):
        script = 'require(process.argv[1]); require(process.argv[2]); console.log(HomeStatus.render(JSON.parse(process.argv[3])));'
        result = subprocess.run(['node', '-e', script, str(STATIC/'html.js'), str(STATIC/'home-status.js'), json.dumps(data)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    NOW = 1_800_000_000  # a fixed "now" (seconds) so "2h ago" is predictable

    def job(self, id, title, status, hours_ago, label=None, group="needs_you"):
        return {'id': id, 'title': title, 'status': status, 'updated': self.NOW - hours_ago * 3600,
                'state': {'group': group, 'label': label or status}}

    def test_only_the_last_commit_and_job_runs_one_line_each(self):
        html = self.render({'git': {'last_commit': 'abc123 Fix score (Ann, 4 hours ago)', 'last_commit_at': '2026-11-10T15:42:00-05:00'},
                            'jobs': [self.job('j1', 'Add controls', 'failed', 2, 'Failed'),
                                     self.job('j2', 'Add score', 'review-needed', 5, 'Ready to merge'),
                                     self.job('j3', 'Not started', 'planned', 1, 'Planned')],
                            'runs': [], 'now': self.NOW})
        self.assertIn('<strong>Last commit</strong><span><span title="abc123 by Ann">Fix score</span> <span class="muted">· Nov 10', html)
        self.assertIn('<strong>Last job run</strong><span><a href="#/jobs/j1">Add controls</a> <span class="muted">· Failed · 2h ago</span>', html)
        self.assertIn('<strong>Last success</strong><span><a href="#/jobs/j2">Add score</a> <span class="muted">· Ready to merge · 5h ago</span>', html)
        self.assertNotIn('Not started', html)  # a planned job hasn't run
        for gone in ('About', 'Suggestions', 'home-suggestion', 'Changes', 'uncommitted'):
            self.assertNotIn(gone, html)

    def test_the_success_line_is_left_out_when_the_last_run_succeeded(self):
        html = self.render({'jobs': [self.job('j2', 'Add score', 'completed', 1, 'Completed', 'done')], 'now': self.NOW})
        self.assertIn('Last job run', html)
        self.assertNotIn('Last success', html)

    def test_empty_project_says_so_and_invents_nothing(self):
        html = self.render({'jobs': [], 'runs': []})
        self.assertIn('No commits yet', html)
        self.assertIn('No jobs have run yet', html)
        self.assertNotIn('Last success', html)

    def test_the_pill_says_what_needs_you_and_links_to_it(self):
        html = self.render({'jobs': [self.job('j1', 'A', 'planned', 1, 'Pending approval')], 'runs': []})
        self.assertIn('<a class="pill attention" href="#/?filter=needs_you">1 job needs you</a>', html)

    def test_commit_and_job_text_are_escaped(self):
        html = self.render({'git': {'last_commit': '<img src=x onerror=bad()>'},
                            'jobs': [self.job('j1', '<script>bad()</script>', 'failed', 1)]})
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img ', html)
        self.assertIn('&lt;', html)


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class HomeIntegrationTests(unittest.TestCase):
    def test_home_renders_the_card_first_with_old_backend_and_removes_the_old_link(self):
        script = r"""
const fs=require('fs'),vm=require('vm');
const dir=process.argv[1];require(dir+'/html.js');require(dir+'/home-status.js');
const source=fs.readFileSync(dir+'/app.js','utf8');
const context={HomeStatus,URLSearchParams,esc:Html.escape,setupState:{complete:true},loadSetup:async()=>{},getBackendUrl:()=>'',
 state:{project:{root:'/sample',name:'Snake',branch:'main',branches:[],dirty_files:42,machine_count:1,model_count:1},runs:[],you:{role:'owner'}},pages:{},
 api:async path=>{if(path==='jobs')return {jobs:[]};if(path==='inbox')return {elsewhere:[]};if(path==='product')return {sections:[]};if(path==='jobs/archived')return {archived:[]};throw Error('older backend');},
 sortJobs:x=>x,homeProjectTitle:p=>p.name,homeHealthIndicator:()=>'',setupState:{complete:true},
 productStripHtml:()=>'<section id="product-strip"></section>',previewCard:()=>'',elsewhereLine:()=>'',HOME_JOBS_SHOWN:10};
vm.createContext(context);
let start=source.indexOf('function statusLine('),end=source.indexOf('\n}\n',start)+3;
vm.runInContext(source.slice(start,end),context);
start=source.indexOf('pages.home = async');end=source.indexOf('function jobHeaderActions(',start);
vm.runInContext(source.slice(start,end),context);
(async()=>{const page=await context.pages.home([],new URLSearchParams());console.log(JSON.stringify({html:page.html,sub:page.sub}));})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(['node', '-e', script, str(STATIC)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        page = json.loads(result.stdout)
        self.assertLess(page['html'].index('id="home-project-status"'), page['html'].index('id="product-strip"'))
        self.assertNotIn('uncommitted', page['sub'])
        self.assertIn('No jobs have run yet', page['html'])
        self.assertNotIn('href="#/new?', page['html'])  # no suggestions any more


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class HomeProjectSwitchTests(unittest.TestCase):
    def test_switching_projects_discards_in_flight_home_results(self):
        script = r"""
const fs=require('fs'),vm=require('vm'),dir=process.argv[1];
require(dir+'/html.js');require(dir+'/home-status.js');
const source=fs.readFileSync(dir+'/app.js','utf8');
const context={HomeStatus,URLSearchParams,esc:Html.escape,setupState:{complete:true},loadSetup:async()=>{},getBackendUrl:()=>'',
 state:{project:{root:'/A',name:'A',branches:[]},runs:[],you:{role:'owner'}},pages:{},
 api:async path=>{if(path==='jobs'){context.state.project={root:'/B',name:'B',branches:[]};return {jobs:[]};}if(path==='inbox')return {elsewhere:[]};if(path==='product')return {sections:[]};if(path==='jobs/archived')return {archived:[]};return {};},
 sortJobs:x=>x,homeProjectTitle:p=>p.name,homeHealthIndicator:()=>'',statusLine:()=>'',
 productStripHtml:()=>'',elsewhereLine:()=>'',HOME_JOBS_SHOWN:10};
vm.createContext(context);
const start=source.indexOf('pages.home = async'),end=source.indexOf('function jobHeaderActions(',start);
vm.runInContext(source.slice(start,end),context);
(async()=>console.log(JSON.stringify(await context.pages.home([],new URLSearchParams()))))().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(['node','-e',script,str(STATIC)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout), {'stale': True})
