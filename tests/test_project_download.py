"""Exercise the real picker download controller without network calls or real cloning."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'orchestrator/web/static'

SCRIPT = r"""
const assert = require('assert/strict');
require(process.argv[1]); require(process.argv[2]);
const scenario = process.argv[3];
const deferred = () => {let resolve, reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject};};
const settle = () => new Promise(setImmediate);
const clone = deferred(), activation = deferred(), opening = deferred();
let backend = 'mac-A', starts = 0, currentRoot = '/old', waitCalls = 0;
const events = [], registrations = [], activations = [];
let failWait = scenario === 'retry';
const picker = ProjectPicker.createDownloads({
  backend: () => backend,
  start: async repo => {starts++; return {task: 'clone-1'};},
  wait: async (task, options) => {
    waitCalls++;
    assert.equal(task.task, 'clone-1');
    assert.ok(options.timeoutMs >= 15 * 60 * 1000);
    if (failWait) {failWait=false; throw new Error('Network interrupted');}
    return clone.promise;
  },
  register: async p => {registrations.push(p.root);},
  activate: async p => {
    activations.push(p.root);
    if (p.root === '/new' && scenario === 'activation-race') await activation.promise;
    if (p.root === '/first' && scenario === 'selection-error') await activation.promise;
    currentRoot = p.root;
    return {project: p};
  },
  ready: async (op, isCurrent) => {
    if (scenario === 'lifecycle') await opening.promise;
    assert.ok(isCurrent());
  },
  change: async (op, started) => {
    events.push({name:op.name, phase:op.phase, started:!!started});
    if (scenario === 'backend-before-start' && started) await opening.promise;
  },
});
(async () => {
  if (scenario === 'markup') {
    const html = ProjectPicker.downloadMarkup({name:'New <project>',repo:'me/new',phase:'downloading'});
    assert.ok(html.includes('<progress'));
    assert.ok(!/<progress[^>]*\bvalue=/.test(html));
    assert.ok(html.includes('aria-label='));
    assert.ok(html.includes('data-project-download'));
    assert.ok(!html.includes('New <project>'));
    const failed = ProjectPicker.downloadMarkup({name:'New',repo:'me/new',phase:'error',error:'No <access>'});
    assert.ok(failed.includes('role="alert"'));
    assert.ok(failed.includes('data-retry-project-download'));
    assert.ok(!failed.includes('<progress'));
    process.stdout.write('ok'); return;
  }
  if (scenario === 'selection-error') {
    const first=picker.select('/first').then(result=>({result}),error=>({error})); await settle();
    const second=picker.select('/other');
    activation.reject(new Error('Old request failed'));
    assert.equal((await first).error,undefined);
    assert.ok((await second).isCurrent());
    assert.equal(currentRoot,'/other');
    process.stdout.write('ok');return;
  }
  const first = picker.download('me/new');
  assert.equal(picker.pending().name, 'new');
  assert.equal(picker.pending().phase, 'downloading');
  assert.equal(events[0].started, true); // Home changes before cloning completes.
  await settle();
  if (scenario === 'backend-before-start') {
    backend='mac-B';opening.resolve();await first;
    assert.equal(starts,0);assert.deepEqual(registrations,[]);assert.deepEqual(activations,[]);
  } else if (scenario === 'stale-selection') {
    const selected = await picker.select('/other');
    assert.ok(selected.isCurrent());
    clone.resolve({root:'/new',name:'New'}); await first;
    assert.equal(currentRoot, '/other');
    assert.deepEqual(activations, ['/other']);
    assert.deepEqual(registrations, ['/new']);
    assert.equal(picker.pending(), null);
  } else if (scenario === 'backend-change') {
    backend='mac-B';
    assert.equal(picker.pending(), null);
    clone.resolve({root:'/new',name:'New'}); await first;
    assert.deepEqual(registrations, []); assert.deepEqual(activations, []);
  } else if (scenario === 'activation-race') {
    clone.resolve({root:'/new',name:'New'}); await settle();
    const second=picker.select('/other'); await settle();
    activation.resolve(); await Promise.all([first,second]);
    assert.equal(currentRoot,'/other');
    assert.deepEqual(activations,['/new','/other']);
    assert.equal(picker.pending(),null);
  } else if (scenario === 'reselect') {
    await picker.select('/other');
    const again=picker.download('me/new');await settle();
    assert.equal(starts,1);
    clone.resolve({root:'/new',name:'New'});await Promise.all([first,again]);
    assert.deepEqual(registrations,['/new']);
    assert.equal(currentRoot,'/new');
    assert.equal(picker.pending().phase,'loading');
  } else if (scenario === 'retry') {
    await first;
    const failed=picker.pending(); assert.equal(failed.phase,'error');
    const retry=picker.download('me/new',failed);
    clone.resolve({root:'/new',name:'New'}); await retry;
    assert.equal(starts,1); assert.equal(waitCalls,2); // Resume the same task after a network error.
    assert.equal(picker.pending().phase,'loading');
  } else {
    const same=picker.download('me/new'); assert.equal(same,first);
    clone.resolve({root:'/new',name:'New'}); await settle();
    assert.equal(picker.pending().phase,'preparing');
    opening.resolve(); await first;
    const op=picker.pending(); assert.equal(op.phase,'loading');
    assert.equal(currentRoot,'/new'); assert.equal(starts,1);
    assert.ok(picker.complete(op)); assert.equal(picker.pending(),null);
    assert.deepEqual(events.map(e=>e.phase),['downloading','preparing','loading']);
  }
  process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class ProjectDownloadTests(unittest.TestCase):
    def test_an_old_route_failure_does_not_fail_the_new_download(self):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');let rejectOld,active=null,failed=0,rendered='',calls=0;
const old=new Promise((resolve,reject)=>{rejectOld=reject;});
const node={open:false,dataset:{},classList:{add(){},remove(){}},focus(){},setAttribute(){},removeAttribute(){}};
const context={URLSearchParams,signingIn:false,projectSwitch:null,RouteAliases:{redirect:()=>null},history:{replaceState(){}},location:{hash:'#/'},
 navigationDrawer:{close(){}},cleanup:[],$:()=>node,document:{body:{dataset:{},classList:{add(){},remove(){}}},querySelectorAll:()=>[]},
 view:node,state:{project:{root:'/old'}},parseHash:()=>({parts:[]}),trackNavigation(){},renderSetupFab(){},routeToken:0,
 resolveRoute:()=>({page:'home',args:[],query:new URLSearchParams(),nav:'home'}),routeParent:()=>null,
 current:{},setTimeout:()=>1,clearTimeout(){},apply:r=>{rendered=r.title;},setHeader(){},loadingSkeleton:()=>'',
 projectDownloads:{pending:()=>active,fail:async()=>{failed++;return true;}},
 pages:{home:()=>++calls===1?old:Promise.resolve({title:'New project',html:'loading'})}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function route()'),source.indexOf('// Live pages refresh in place')),context);
(async()=>{
 const previous=context.route();active={phase:'downloading',task:'valid-task'};
 await context.route();rejectOld(Object.assign(new Error('Old project failed'),{status:404}));await previous;
 assert.equal(failed,0);assert.equal(active.task,'valid-task');assert.equal(rendered,'New project');
 process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(STATIC / 'app.js')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_home_hides_previous_project_and_keeps_progress_until_new_home_data_is_ready(self):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),dir=process.argv[1];
require(dir+'/html.js'); require(dir+'/home-status.js'); require(dir+'/project-picker.js');
const source=fs.readFileSync(dir+'/app.js','utf8');
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const cloning=deferred(),oldJobs=deferred(),newJobs=deferred(),settle=()=>new Promise(setImmediate);
const context={HomeStatus,URLSearchParams,esc:Html.escape,setupState:{complete:true},loadSetup:async()=>{},getBackendUrl:()=>'',
 state:{project:{root:'/old',name:'Old project',branches:[]},runs:[],you:{role:'owner'}},pages:{},
 api:async path=>{if(path==='jobs')return context.state.project.root==='/old'?oldJobs.promise:newJobs.promise;
   if(path==='inbox')return {elsewhere:[]};if(path==='product')return null;if(path==='jobs/archived')return {archived:[]};return {};},
 sortJobs:x=>x,homeProjectTitle:p=>p.name,homeHealthIndicator:()=>'',statusLine:()=>'',
 productStripHtml:()=>'',elsewhereLine:()=>'',HOME_JOBS_SHOWN:10};
const picker=ProjectPicker.createDownloads({backend:()=>'',start:async()=>({task:'one'}),wait:()=>cloning.promise,
 register:async()=>{},activate:async p=>({project:p}),ready:async op=>{context.state.project={...op.project,branches:[]};},change:async()=>{}});
context.projectDownloads=picker;
context.projectDownloadPage=op=>({title:op.name,html:ProjectPicker.downloadMarkup(op)});
context.finishProjectDownload=op=>picker.complete(op);
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('pages.home = async'),source.indexOf('function jobHeaderActions(')),context);
(async()=>{
 const old=context.pages.home([],new URLSearchParams());await settle();
 const download=picker.download('me/new');
 const pending=await context.pages.home([],new URLSearchParams());
 assert.equal(pending.title,'new');assert.ok(pending.html.includes('<progress'));
 assert.ok(!pending.html.includes('Old project'));
 oldJobs.resolve({jobs:[]});assert.equal((await old).stale,true);
 cloning.resolve({root:'/new',name:'New project'});await download;
 const home=context.pages.home([],new URLSearchParams());await settle();
 assert.equal(picker.pending().phase,'loading'); // Clone finished, but Home data is still in flight.
 newJobs.resolve({jobs:[]});const loaded=await home;
 assert.equal(loaded.title,'New project');assert.ok(loaded.html.includes('home-project-status'));
 assert.equal(picker.pending(),null);
 process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(STATIC)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'ok')

    def test_download_lifecycle_and_selection_races(self):
        for scenario in ('lifecycle', 'stale-selection', 'activation-race', 'selection-error', 'reselect', 'backend-change', 'backend-before-start', 'retry', 'markup'):
            with self.subTest(scenario=scenario):
                result = subprocess.run(['node', '-e', SCRIPT, str(STATIC / 'html.js'),
                                         str(STATIC / 'project-picker.js'), scenario], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'ok')

    def test_download_watcher_keeps_waiting_after_seven_minutes_and_stops_on_backend_change(self):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
let now=0,calls=0;
const context={DOMException,Date:{now:()=>now},setTimeout:fn=>{now+=60000;queueMicrotask(fn);},
 api:async()=>++calls<9?{status:'running'}:{status:'done',result:'ready'}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function waitForTask('),source.indexOf('async function streamTask(')),context);
(async()=>{
 assert.equal(await context.waitForTask({task:'one'},{timeoutMs:16*60*1000}),'ready');
 const before=calls;
 await assert.rejects(context.waitForTask({task:'two'},{isCurrent:()=>false}),{name:'AbortError'});
 assert.equal(calls,before);
 process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(STATIC / 'app.js')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
