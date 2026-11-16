"""Structured Connect progress and safe browser approval links."""
import base64
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from orchestrator import ai_providers

MODULE=Path(__file__).resolve().parents[1]/'orchestrator/web/static/tool-connect.js'

@unittest.skipUnless(shutil.which('node'),'Node.js required')
class ToolConnectUiTests(unittest.TestCase):
    def state(self,text,provider=''):
        result=subprocess.run(['node','-e','require(process.argv[1]);console.log(JSON.stringify(ToolConnect.progressState(JSON.parse(process.argv[2]),undefined,process.argv[3])));',str(MODULE),json.dumps(text),provider],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_progress_skips_no_steps_and_keeps_ready_state(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"check","detail":"Checking"}\n@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Approve in browser"}\n@@ORCHESTRATOR_CONNECT@@{"step":"ready","detail":"Connected"}\n')
        self.assertEqual(result['step'],'ready')
        self.assertEqual(result['detail'],'Connected')

    def test_only_real_https_provider_links_are_offered(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Sign in"}\nhttps://auth.openai.com/authorize?code=ABC javascript:alert(1) https://auth.openai.com.evil.test/steal https://evil.test/openai.com\n')
        self.assertEqual(result['urls'],['https://auth.openai.com/authorize?code=ABC'])

    # What each signable tool prints when it starts its sign-in: its link, and whether it then waits for a pasted code.
    SIGN_INS={
        'claude':('https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz','Paste code here if prompted > ',True),
        'codex':('https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_1','Waiting for the browser to finish sign-in...',False),
        'agy':('https://accounts.google.com/o/oauth2/v2/auth?client_id=1&scope=x','',False),
        'pool':('https://platform.poolside.ai/login?cli=1','',False),
    }

    def test_every_tool_with_a_sign_in_is_known_to_the_dialog(self):
        result=subprocess.run(['node','-e','require(process.argv[1]);console.log(JSON.stringify(Object.keys(ToolConnect.providers).sort()));',str(MODULE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(json.loads(result.stdout),sorted(ai_providers.SIGN_IN_COMMANDS))
        self.assertEqual(sorted(self.SIGN_INS),sorted(ai_providers.SIGN_IN_COMMANDS))

    def test_each_tools_link_is_offered_whole_and_only_code_prompts_get_a_paste_box(self):
        for provider,(url,prompt,paste) in self.SIGN_INS.items():
            with self.subTest(provider=provider):
                result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\nIf the browser did not open, visit: '+url+'\n'+prompt,provider)
                self.assertEqual(result['urls'],[url])
                self.assertEqual(result['code'],'')
                self.assertEqual(result['needsCode'],paste)

    def test_a_tool_that_asks_for_a_code_gets_the_paste_box_whatever_it_is(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\nOpen https://accounts.google.com/o/oauth2/auth?x=1\nEnter the authorization code: ','agy')
        self.assertTrue(result['needsCode'])
        self.assertFalse(self.state('@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\nEnter the authorization code: ')['needsCode'])  # no link yet

    def test_a_device_code_is_shown_not_pasted(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\nGo to https://auth.openai.com/codex/device and enter ABCD-1234\n','codex')
        self.assertEqual(result['code'],'ABCD-1234')
        self.assertFalse(result['needsCode'])

    def test_failed_job_text_finds_the_tool_to_sign_in_to(self):
        cases={'Failed to authenticate: 401 OAuth access token has expired (claude)':'claude','codex: session expired, refresh failed':'codex',
               'openai auth error':'codex','Antigravity CLI: please sign in again':'agy','agy: failed to authenticate':'agy','pool login required':'pool',
               'poolside session expired':'pool'}
        script='require(process.argv[1]);const c=JSON.parse(process.argv[2]);console.log(JSON.stringify(Object.keys(c).map(k=>(ToolConnect.providerFromText(k)||{}).id||null)));'
        result=subprocess.run(['node','-e',script,str(MODULE),json.dumps(cases)],capture_output=True,text=True,timeout=10)
        self.assertEqual(json.loads(result.stdout),list(cases.values()))
        script='require(process.argv[1]);console.log(JSON.stringify([ToolConnect.providerFromText("threadpool exhausted"),ToolConnect.providerFromText("")]));'
        result=subprocess.run(['node','-e',script,str(MODULE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(json.loads(result.stdout),[None,None])

    DIALOG='''
const [, modulePath, provider, outputB64] = process.argv;
const els = {};
const make = id => els[id] = els[id] || {id, hidden: false, textContent: "", innerHTML: "", value: "", className: "", open: false, dataset: {},
  querySelectorAll: () => [], querySelector: () => ({className: "", querySelector: () => ({textContent: ""})})};
["dialog-ok","connect-agree","connect-continue","connect-stop","connect-input","connect-send","connect-code","connect-code-send","connect-code-row",
 "connect-status","connect-browser","connect-output","connect-details"].forEach(make);
const calls = [];
let polls = 0;
global.document = {getElementById: make, querySelector: () => ({className: "", querySelector: () => ({textContent: ""})}), querySelectorAll: () => []};
global.Html = {escape: s => String(s)};
global.atob = s => Buffer.from(s, "base64").toString("binary");
require(modulePath);
const api = async (path, opts = {}) => {
  calls.push([path, opts.body]);
  if (path === "runs") return {run: {id: "r1"}};
  if (path.includes("/output")) return {offset: 1, data: polls++ ? "" : outputB64, finished: false};
  return {};
};
let close; const dialog = new Promise(resolve => close = resolve);
ToolConnect.open(provider, provider, {api, formDialog: () => dialog});
(async () => {
  await new Promise(r => setTimeout(r, 200));
  const shown = !els["connect-code-row"].hidden;
  if (shown) {
    els["connect-code"].value = " CODE-123 ";
    await els["connect-code-send"].onclick();
  }
  await new Promise(r => setTimeout(r, 50));
  close();
  console.log(JSON.stringify({shown, browser: els["connect-browser"].innerHTML, link: els["connect-browser"].innerHTML.includes("readonly"), input: calls.filter(c => c[0].endsWith("/input")).map(c => c[1].data), field: els["connect-code"].value, started: calls[0]}));
  process.exit(0);
})();
'''

    def drive_dialog(self,provider,output):
        result=subprocess.run(['node','-e',self.DIALOG,str(MODULE),provider,base64.b64encode(output.encode()).decode()],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_dialog_for_each_tool_shows_the_link_and_sends_a_pasted_code_without_the_terminal(self):
        for provider,(url,prompt,paste) in self.SIGN_INS.items():
            with self.subTest(provider=provider):
                seen=self.drive_dialog(provider,'@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\n'+url+'\n'+prompt)
                self.assertEqual(seen['started'],['runs',{'action':'ai_connect','params':{'provider':provider,'title':f'Connect {provider}'}}])
                self.assertTrue(seen['link'])  # the whole link, copyable, besides the button
                self.assertEqual(seen['shown'],paste)
                self.assertEqual(seen['input'],['CODE-123\r'] if paste else [])  # trimmed, and Enter appended
                self.assertEqual(seen['field'],'')  # the box empties once sent

    def test_installer_terms_require_explicit_agreement(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"install","detail":"Installing"}\nAgree to terms? [Y/n]')
        self.assertTrue(result['needsAgreement'])
        self.assertEqual(result['step'],'install')

    def test_claude_osc8_claude_com_url_and_code_prompt_are_detected(self):
        url = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"
        raw = (
            '@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\n'
            f"If the browser didn't open, visit: \x1b]8;;{url}\x07\x1b[94m{url}\x1b[39m\x1b]8;;\x07\n"
            "Paste code here if prompted > "
        )
        result = self.state(raw, "claude")
        self.assertEqual(result["urls"], [url])
        self.assertTrue(result["needsCode"])
        self.assertNotIn("\x1b]8;;", result["output"])
        seen = self.drive_dialog("claude", raw)
        self.assertIn(url, seen["browser"])
        self.assertTrue(seen["shown"])
        self.assertEqual(seen["input"], ["CODE-123\r"])

    def test_malformed_markers_do_not_break_progress_or_turn_into_instructions(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@bad json\n@@ORCHESTRATOR_CONNECT@@{"step":"shell","detail":"run me"}\n')
        self.assertEqual(result['step'],'check')
        self.assertEqual(result['urls'],[])
