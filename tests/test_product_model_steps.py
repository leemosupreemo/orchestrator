"""Model choice belongs to an AI operation, not the manual Product entry points."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1]/'orchestrator/web/static'

@unittest.skipUnless(shutil.which('node'),'Node.js required')
class ProductModelStepsTests(unittest.TestCase):
    def run_js(self, body):
        script=r'''
const fs=require('fs'),vm=require('vm'),dir=process.argv[1];require(dir+'/html.js');
const source=fs.readFileSync(dir+'/app.js','utf8');
const context={esc:Html.escape,prdSnippet:s=>s};vm.createContext(context);
function load(name){const start=source.indexOf('function '+name+'(');if(start<0)throw Error('Missing '+name);const end=source.indexOf('\n}\n',start)+3;vm.runInContext(source.slice(start,end),context);}
load('productStripHtml');
'''+body
        result=subprocess.run(['node','-e',script,str(STATIC)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_manual_entry_points_have_no_model_picker(self):
        html=self.run_js('console.log(JSON.stringify(context.productStripHtml({sections:[],can_draft:true,available_models:[{id:"a",label:"A"}]})));')
        self.assertNotIn('<select',html)
        self.assertIn('Write it',html)
        self.assertIn('Import PRD',html)

    def test_ai_step_model_field_uses_available_models_and_escapes_labels(self):
        html=self.run_js('load("prdModelField");console.log(JSON.stringify(context.prdModelField({model:"b",available_models:[{id:"a",label:"<script>"},{id:"b",label:"Model B"}]},"draft-model")));')
        self.assertIn('name="model"',html)
        self.assertIn('value="b" selected',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>',html)
