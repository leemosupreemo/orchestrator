"""Run the package action renderer; unsupported updates offer an explicit AI plan."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

STATIC=Path(__file__).resolve().parents[1]/'orchestrator/web/static'

@unittest.skipUnless(shutil.which('node'),'Node.js required')
class PackageUpdateUiTests(unittest.TestCase):
    def run_js(self,body):
        script=r'''
const fs=require('fs'),vm=require('vm'),dir=process.argv[1];require(dir+'/html.js');
const source=fs.readFileSync(dir+'/app.js','utf8');
const c={esc:Html.escape,attrJSON:x=>Html.escape(JSON.stringify(x))};vm.createContext(c);
function load(name){const a=source.indexOf('function '+name+'(');if(a<0)throw Error('Missing '+name);const b=source.indexOf('\n}\n',a)+3;vm.runInContext(source.slice(a,b),c);}
load('packageJobHref');load('packageUpdateAction');load('moreMenu');
const item={name:'react',ecosystem:'npm',manifest:'packages/chosen/package.json',version:'^18'};
const data={ecosystems:{npm:'JavaScript'},features:[],installed:[{ecosystem:'npm',manifest:'other/package.json'}]};
'''+body
        r=subprocess.run(['node','-e',script,str(STATIC)],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        return json.loads(r.stdout)

    def test_supported_native_action_does_not_offer_ai(self):
        action=self.run_js("item.update={supported:true,manager:'npm'};console.log(JSON.stringify(c.packageUpdateAction(item,data)));")
        self.assertEqual(action[0],'Update')
        self.assertIn('data-package-update',action[1])
        self.assertNotIn('data-href',action[1])

    def test_unsupported_action_explains_limitation_and_preserves_selected_manifest(self):
        out=self.run_js("item.update={supported:false,reason:'Workspace <scope> unavailable'};const a=c.packageUpdateAction(item,data);console.log(JSON.stringify({a,html:c.moreMenu([a],{compact:true})}));")
        self.assertIn('AI',out['a'][0])
        self.assertNotIn('data-package-update',out['a'][1])
        self.assertIn('packages%2Fchosen%2Fpackage.json',out['a'][1])
        self.assertNotIn('other%2Fpackage.json',out['a'][1])
        self.assertIn('Workspace &lt;scope&gt; unavailable',out['html'])
