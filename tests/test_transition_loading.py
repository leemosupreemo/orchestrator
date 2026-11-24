"""Immediate loading feedback through the app's actual transition functions."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

APP = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class TransitionLoadingTests(unittest.TestCase):
    def run_js(self, body):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const classes=()=>({add(){},remove(){},contains(){return false;}});
const elements=new Map();
const element=()=>({innerHTML:'',hidden:false,dataset:{},classList:classes(),setAttribute(){},removeAttribute(){}});
const view=element(),body=element();
const headers=[],routes=[],notices=[],requests=[];
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {resolve,reject,promise};};
const context={assert,view,document:{body,title:'',querySelector:()=>element()},
 $:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);},
 esc:s=>String(s??'').replaceAll('<','&lt;'),Notifications:{tabTitle:s=>s},
 state:{project:{name:'Old',root:'/old',recent:[{name:'New',root:'/new'}]}},
 navigationDrawer:{close(){}},cleanup:[],routeToken:0,projectSwitch:null,setupFetched:1,
 getBackendUrl:()=> 'mac-A',setHeader:h=>headers.push(h),
 toast:(...args)=>notices.push(args),route:async()=>routes.push(context.state.project.root),
 refreshState:async()=>{},location:{hash:'#/'},
 projectDownloads:{select:root=>{const d=deferred();requests.push({root,...d});return d.promise;}},
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const SKELETON_KINDS'),source.indexOf('let routeToken')),context);
const signing=source.slice(source.indexOf('function showSigningIn('),source.indexOf('// ---------------------------------------------------------------- account (hosted app)'));
vm.runInContext(signing,context);
const start=source.indexOf('async function switchProject(');
if(start>=0)vm.runInContext(source.slice(start,source.indexOf('// Shown from the click on a provider',start)),context);
''' + body
        result = subprocess.run(["node", "-e", script, str(APP)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_signing_in_immediately_renders_a_skeleton_with_its_status(self):
        out = self.run_js('''
context.showSigningIn('Opening Google…');
const withoutCancel = view.innerHTML;
let cancelled = false;
view.querySelector = (sel) => sel === '[data-cancel-connect]' ? { addEventListener: (_ev, fn) => fn({ preventDefault(){} }) } : null;
context.showSigningIn('Opening MacBook…', { onCancel: () => { cancelled = true; } });
console.log(JSON.stringify({
  skeleton: withoutCancel.includes('skeleton-page'),
  noCardBrand: !withoutCancel.includes('signing-in-brand') && !withoutCancel.includes('brand-mark'),
  label: withoutCancel.includes('Opening Google…'),
  noCloseWithoutCancel: !withoutCancel.includes('signing-in-close'),
  hasTopRightClose: view.innerHTML.includes('class="dialog-close-btn signing-in-close"') && view.innerHTML.includes('aria-label="Cancel"'),
  noBottomCancelText: !view.innerHTML.includes('>Cancel</button>'),
  cancelled,
}));
''')
        self.assertEqual(out, {
            "skeleton": True,
            "noCardBrand": True,
            "label": True,
            "noCloseWithoutCancel": True,
            "hasTopRightClose": True,
            "noBottomCancelText": True,
            "cancelled": True,
        })

    def test_project_switch_renders_before_activation_and_only_latest_choice_finishes(self):
        out = self.run_js('''
(async()=>{
 const a=context.switchProject('/new');
 const loading=view.innerHTML.includes('skeleton-page');
 const b=context.switchProject('/other');
 requests[0].resolve({response:{project:{root:'/new',name:'New'}},isCurrent:()=>false});await a;
 const staleRoutes=routes.length;
 requests[1].resolve({response:{project:{root:'/other',name:'Other'}},isCurrent:()=>true});await b;
 console.log(JSON.stringify({loading,staleRoutes,routes,selected:context.state.project.root,pending:context.projectSwitch}));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
        self.assertEqual(out, {"loading": True, "staleRoutes": 0, "routes": ["/other"], "selected": "/other", "pending": None})

    def test_failed_switch_restores_the_page_and_reports_the_error(self):
        out = self.run_js('''
(async()=>{
 const pending=context.switchProject('/new');requests[0].reject(new Error('Cannot open project'));await pending;
 console.log(JSON.stringify({routes,notices,pending:context.projectSwitch}));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
        self.assertEqual(out, {"routes": ["/old"], "notices": [["Cannot open project", True]], "pending": None})

    def test_an_inflight_poll_cannot_replace_the_login_or_project_skeleton(self):
        for transition in ("signin", "project"):
            with self.subTest(transition=transition):
                out = self.run_js('''
const render=deferred();context.LIVE=new Set(['home']);context.signingIn=false;
context.current={page:'home',args:[],query:{},rendered:''};context.loadSetup=async()=>{};
context.view.querySelector=()=>null;context.view.contains=()=>false;context.document.querySelector=()=>null;
context.pages={home:()=>render.promise};context.signatureOf=r=>r.html;context.apply=r=>{view.innerHTML=r.html;};
vm.runInContext(source.slice(source.indexOf('async function tick()'),source.indexOf('window.addEventListener("hashchange"')),context);
(async()=>{
 const poll=context.tick();await new Promise(setImmediate);
 const transition=TRANSITION;
 if(transition==='signin')context.showSigningIn('Signing in…');else context.switchProject('/new');
 render.resolve({html:'OLD PROJECT'});await poll;
 console.log(JSON.stringify({skeleton:view.innerHTML.includes('skeleton-page')}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace("TRANSITION", json.dumps(transition)))
                self.assertEqual(out, {"skeleton": True})

    def test_launch_server_transitions_immediately_to_run_skeleton_and_restores_on_error(self):
        script = r'''
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const classes = new Set();
const view = { innerHTML: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
const headers = [], toasts = [], routes = [];
let pendingApi;
const context = {
  document: {
    body: { dataset: {}, classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), has: (c) => classes.has(c) } },
  },
  location: { hash: '#/' },
  view,
  cleanup: [() => {}],
  routeToken: 1,
  state: { actions: {}, runs: [] },
  current: { page: 'home', args: [] },
  setHeader: (h) => headers.push(h),
  loadingSkeleton: (page) => `<div class="skeleton-page" data-kind="${page}"></div>`,
  terminalSize: () => ({ cols: 80, rows: 24 }),
  tryAutoFailoverAction: async () => false,
  trackWebEvent: () => {},
  refreshState: async () => {},
  route: () => routes.push('routed'),
  toast: (m, err) => toasts.push({ m, err }),
  api: () => new Promise((resolve, reject) => { pendingApi = { resolve, reject }; }),
};
vm.createContext(context);
const skelStart = source.indexOf('let runTransitioning = false;');
const skelEnd = source.indexOf('function formDialog(', skelStart);
vm.runInContext(source.slice(skelStart, skelEnd), context);
(async () => {
  const runPromise = context.runAction('server_start', { id: 'srv-1', machine: 'local' }, { skipConfirm: true, optimisticRunTitle: 'Starting Game Server…' });
  const immediate = {
    page: context.document.body.dataset.page,
    loadingClass: classes.has('route-loading'),
    skeleton: view.innerHTML.includes('data-kind="run"'),
    header: headers[headers.length - 1]?.title,
    transitioning: vm.runInContext('runTransitioning', context),
  };
  await new Promise(setImmediate);
  pendingApi.resolve({ run: { id: 'run-42' } });
  await runPromise;
  const afterSuccess = { hash: context.location.hash, transitioning: vm.runInContext('runTransitioning', context) };

  const failPromise = context.runAction('server_start', { id: 'srv-1', machine: 'local' }, { skipConfirm: true, optimisticRunTitle: 'Starting Game Server…' });
  await new Promise(setImmediate);
  pendingApi.reject(new Error('Docker failed'));
  await failPromise;
  console.log(JSON.stringify({ immediate, afterSuccess, routes: routes.length, errToast: toasts[0]?.m }));
})().catch((e) => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", script, str(APP)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out, {
            "immediate": {
                "page": "run",
                "loadingClass": True,
                "skeleton": True,
                "header": "Starting Game Server…",
                "transitioning": True,
            },
            "afterSuccess": {"hash": "#/runs/run-42", "transitioning": False},
            "routes": 1,
            "errToast": "Docker failed",
        })
        app_src = APP.read_text(encoding="utf-8")
        self.assertIn("optimisticRunTitle: `${verb} ${serverLabel(server)}…`", app_src)
