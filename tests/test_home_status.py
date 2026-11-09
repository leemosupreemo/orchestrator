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
        path.write_text(path.read_text().replace('old requirement', 'new requirement'))
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

    def test_card_contains_project_facts_and_three_encoded_prefilled_links(self):
        html = self.render({'project': {'name': 'Snake', 'dirty_files': 4, 'branch': 'main', 'kind': 'web'},
                            'git': {'last_commit': 'abc123 Fix score'}, 'product': {'sections': [{'filled': True, 'body': 'A Nokia snake game'}]},
                            'jobs': [{'id': 'j1', 'title': 'Add controls', 'updated': '2026-11-09', 'state': {'group': 'needs_you', 'label': 'Review'}}],
                            'runs': [], 'suggestions': [{'title': 'Score & pause', 'type': 'feature', 'summary': 'Add score & pause', 'spec': 'Do not start yet.'}]*3})
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def __init__(self):
                super().__init__(); self.urls=[]
            def handle_starttag(self, tag, attrs):
                href=dict(attrs).get('href','')
                if tag=='a' and href.startswith('#/new?'): self.urls.append(href)
        links=Links(); links.feed(html)
        self.assertEqual(len(links.urls), 3)
        from urllib.parse import parse_qs
        self.assertEqual(parse_qs(links.urls[0].split('?',1)[1]), {'type':['feature'],'summary':['Add score & pause'],'spec':['Do not start yet.']})
        for expected in ('A Nokia snake game','abc123 Fix score','4 uncommitted files','Add controls','Needs your attention'):
            self.assertIn(expected, html)

    def test_empty_project_has_useful_fallbacks_and_no_invented_commit(self):
        html = self.render({'project': {'name': 'New project', 'dirty_files': 0}, 'jobs': [], 'runs': []})
        self.assertIn('No commits yet', html)
        self.assertIn('Working tree clean', html)
        self.assertEqual(html.count('href="#/new?'), 3)

    def test_project_and_model_html_are_escaped(self):
        html = self.render({'project': {'name': '<script>bad()</script>'}, 'git': {'last_commit': '<img src=x onerror=bad()>'},
                            'suggestions': [{'title': '<img src=x onerror=bad()>', 'type': 'feature', 'summary': 'x', 'spec': '<script>'}]*3})
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
 productStripHtml:()=>'<section id="product-strip"></section>',elsewhereLine:()=>'',HOME_JOBS_SHOWN:10};
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
        self.assertIn('42 uncommitted files', page['html'])
        self.assertNotIn('uncommitted', page['sub'])
        self.assertEqual(page['html'].count('href="#/new?'), 3)
        self.assertIn('type=coverage', page['html'])


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
