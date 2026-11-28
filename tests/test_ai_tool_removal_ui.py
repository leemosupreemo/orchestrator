import json
from pathlib import Path
import shutil
import subprocess
import unittest

from orchestrator.ai_providers import with_status

STATIC = Path(__file__).resolve().parents[1] / "orchestrator/web/static"


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class AiToolRemovalUITests(unittest.TestCase):
    def test_only_owners_can_uninstall_installed_tools_and_gemini_explains_migration(self):
        ai = with_status(lambda cli: cli in ("gemini", "codex"), lambda cli: False, set())
        script = r'''
require(process.argv[1]+'/html.js');require(process.argv[1]+'/configuration.js');
const ai=JSON.parse(process.argv[2]);
const render=role=>ConfigurationPages.render('ai',{viewer:{role},ai,keys:[]}).html;
console.log(JSON.stringify({owner:render('owner'),member:render('member')}));
'''
        result = subprocess.run(["node", "-e", script, str(STATIC), json.dumps(ai)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = json.loads(result.stdout)
        for provider in ("gemini", "codex"):
            self.assertIn(f'data-ai-uninstall="{provider}"', html["owner"])
        self.assertNotIn('data-ai-uninstall="agy"', html["owner"])
        self.assertNotIn("data-ai-uninstall", html["member"])
        self.assertIn("Legacy · use Antigravity", html["owner"])
        self.assertIn("API keys", html["owner"])
        self.assertIn("Enterprise", html["owner"])
        self.assertIn('data-ai-connect="agy"', html["owner"])

    def test_confirmation_is_required_and_changing_computers_cancels_removal(self):
        for outcome in ("confirm", "cancel", "changed"):
            with self.subTest(outcome=outcome):
                script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),outcome=process.argv[2];
const calls=[];let confirm,backend='mac-A',options;
const button={dataset:{aiUninstall:'codex',aiName:'Codex'},isConnected:true};
const context={getBackendUrl:()=>backend,state:{project:{computer:{name:'My Mac'}}},
 esc:String,formDialog:(title,body,label,opts)=>{options={title,body,label,opts};return new Promise(r=>{confirm=r;});},
 api:async(path,args)=>{calls.push({path,...args});return {task:'one'};},
 waitForTask:async()=>({detail:'Removed',remaining:false}),toast(){},route:async()=>{}};
vm.createContext(context);
const start=source.indexOf('          const onUninstall = async');
vm.runInContext(source.slice(start,source.indexOf('          view.addEventListener("click", onUninstall)',start))+';globalThis.remove=onUninstall;',context);
(async()=>{
 const pending=context.remove({target:{closest:()=>button}});
 assert.equal(calls.length,0);assert.equal(options.opts.danger,true);assert.ok(options.body.includes('My Mac'));
 if(outcome==='changed')backend='mac-B';confirm(outcome!=='cancel');await pending;
 assert.equal(calls.length,outcome==='confirm'?1:0);
 if(calls.length){assert.equal(calls[0].path,'ai-providers/uninstall');assert.equal(calls[0].body.confirmed,true);assert.equal(calls[0].body.provider,'codex');}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
                result = subprocess.run(["node", "-e", script, str(STATIC / "app.js"), outcome], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
