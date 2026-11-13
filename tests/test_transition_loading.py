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
console.log(JSON.stringify({skeleton:view.innerHTML.includes('skeleton-page'),label:view.innerHTML.includes('Opening Google…')}));
''')
        self.assertEqual(out, {"skeleton": True, "label": True})

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
