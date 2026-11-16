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
        'claude':('https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz','Paste code here if prompted > ',True),
        'codex':('https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_1','Waiting for the browser to finish sign-in...',False),
        'agy':('https://accounts.google.com/o/oauth2/auth?client_id=1&scope=x','After authenticating, copy the code displayed in the browser and paste it below:',True),
        'pool':('https://platform.poolside.ai/?referrer=pool','Enter API key\n> paste API key here',True),
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
        for code in ('ABCD-1234', 'LZD9-WM5SU'):
            with self.subTest(code=code):
                result=self.state(f'@@ORCHESTRATOR_CONNECT@@{{"step":"sign_in","detail":"Signing in"}}\nGo to https://auth.openai.com/codex/device and enter {code}\n','codex')
                self.assertEqual(result['code'],code)
                self.assertFalse(result['needsCode'])

    def test_agy_and_pool_menus_osc8_links_and_prompts_work(self):
        # Detail banner alone must not show Continue before the CLI menu renders.
        banner = '@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in to Poolside. \\"Use Poolside Platform for free\\" is already chosen, so press Enter."}\nSigning in to Poolside. "Use Poolside Platform for free" is already chosen, so press Enter.\n'
        self.assertFalse(self.state(banner, 'pool')['needsContinue'])
        pool_menu = banner + 'Log in to Poolside\n> Use Poolside Platform for free (recommended)\nenter submit\n'
        self.assertTrue(self.state(pool_menu, 'pool')['needsContinue'])
        pool_key = pool_menu + 'Opening https://platform.poolside.ai/?referrer=pool\nEnter API key\n> paste API key here\n'
        pool_state = self.state(pool_key, 'pool')
        self.assertEqual(pool_state['urls'], ['https://platform.poolside.ai/?referrer=pool'])
        self.assertTrue(pool_state['needsCode'])
        self.assertFalse(pool_state['needsContinue'])

        agy_menu = '@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Signing in"}\nSelect login method:\n> 1. Google OAuth\nenter Select\n'
        self.assertTrue(self.state(agy_menu, 'agy')['needsContinue'])
        full_google_url = 'https://accounts.google.com/o/oauth2/auth?client_id=1&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&state=F35hz'
        wrapped_prefix = 'https://accounts.google.com/o/oauth2/auth?client_id=1&redirect_uri=https%3A%2F%2Fantigravity'
        agy_auth = agy_menu + f'{wrapped_prefix}\n.google%2Foauth-callback&state=F35hz\n\x1b]8;id=abc;{full_google_url}\x07→ Click here to authenticate\x1b]8;;\x07\nAfter authenticating, copy the code displayed in the browser and paste it below:\n'
        agy_state = self.state(agy_auth, 'agy')
        self.assertEqual(agy_state['urls'], [full_google_url])
        self.assertTrue(agy_state['needsCode'])
        self.assertFalse(agy_state['needsContinue'])

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
["dialog-ok","connect-agree","connect-continue","connect-stop","connect-code","connect-code-send","connect-code-row",
 "connect-status","connect-browser","connect-steps","connect-intro","connect-question","connect-log"].forEach(make);
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
  console.log(JSON.stringify({shown, browser: els["connect-browser"].innerHTML, link: els["connect-browser"].innerHTML.includes("data-connect-copy"), input: calls.filter(c => c[0].endsWith("/input")).map(c => c[1].data), field: els["connect-code"].value, started: calls[0], stepsHidden: els["connect-steps"].hidden, question: els["connect-question"].hidden ? "" : els["connect-question"].textContent, agree: !els["connect-agree"].hidden}));
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
                self.assertTrue(seen['link'])  # Copy link beside the button, to sign in on another device
                self.assertTrue(seen['stepsHidden'])  # signing in shows only what to do, so Submit stays on screen
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
        self.assertNotIn("@@ORCHESTRATOR_CONNECT@@", result["details"])
        self.assertIn(url, result["details"])
        self.assertIn("Paste code here if prompted >", result["details"])
        seen = self.drive_dialog("claude", raw)
        self.assertIn(url, seen["browser"])
        self.assertTrue(seen["shown"])
        self.assertEqual(seen["input"], ["CODE-123\r"])

    def test_stop_signin_stops_running_and_missing_runs_cleanly(self):
        script = '''
const [, modulePath] = process.argv;
const els = {};
const make = id => els[id] = els[id] || {id, hidden: false, disabled: false, textContent: "", innerHTML: "", value: "", className: "", open: false, dataset: {},
  querySelectorAll: () => [], querySelector: () => ({className: "", querySelector: () => ({textContent: ""})})};
["dialog-ok","connect-agree","connect-continue","connect-stop","connect-code","connect-code-send","connect-code-row",
 "connect-status","connect-browser","connect-steps","connect-intro","connect-question","connect-log"].forEach(make);
const stepRows = ["check","prerequisites","install","sign_in","ready"].map(() => ({className: "ai-step", querySelector: () => ({textContent: ""})}));
const stepMap = Object.fromEntries(["check","prerequisites","install","sign_in","ready"].map((k, i) => [`[data-connect-step="${k}"]`, stepRows[i]]));
global.document = {getElementById: make, querySelector: sel => stepMap[sel] || stepRows[0], querySelectorAll: () => stepRows};
global.Html = {escape: s => String(s)};
global.atob = s => Buffer.from(s, "base64").toString("binary");
require(modulePath);
let stopped = false;
const api = async (path) => {
  if (path === "runs") { await new Promise(r => setTimeout(r, 40)); return {run: {id: "r1"}}; }
  if (path === "runs/r1/stop") { stopped = true; return {ok: true}; }
  if (path.includes("/output")) {
    await new Promise(r => setTimeout(r, 20));
    return {offset: 1, data: "", finished: stopped, exit_code: stopped ? -15 : null};
  }
  return {};
};
let close; const dialog = new Promise(resolve => close = resolve);
const done = ToolConnect.open("claude", "Claude", {api, formDialog: () => dialog});
(async () => {
  await new Promise(r => setTimeout(r, 10));
  await els["connect-stop"].onclick();
  const whileStopping = {disabled: els["connect-stop"].disabled, text: els["connect-stop"].textContent, status: els["connect-status"].textContent};
  await done;
  close();
  console.log(JSON.stringify({whileStopping, stopped, stopHidden: els["connect-stop"].hidden, okHidden: els["dialog-ok"].hidden, status: els["connect-status"].textContent, stepClass: stepRows[0].className}));
  process.exit(0);
})();
'''
        result = subprocess.run(["node", "-e", script, str(MODULE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data["whileStopping"]["disabled"])
        self.assertEqual(data["whileStopping"]["text"], "Stopping…")
        self.assertEqual(data["whileStopping"]["status"], "Stopping sign-in…")
        self.assertTrue(data["stopped"])
        self.assertTrue(data["stopHidden"])
        self.assertFalse(data["okHidden"])
        self.assertEqual(data["status"], "Sign-in stopped.")
        self.assertEqual(data["stepClass"], "ai-step")

    def test_malformed_markers_do_not_break_progress_or_turn_into_instructions(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@bad json\n@@ORCHESTRATOR_CONNECT@@{"step":"shell","detail":"run me"}\n')
        self.assertEqual(result['step'],'check')
        self.assertEqual(result['urls'],[])

    def test_the_dialog_has_no_advanced_section_and_shows_what_it_asks_before_agree(self):
        source=MODULE.read_text(encoding='utf-8')
        for gone in ('Sign-in details and advanced input','connect-details','connect-input','readonly value'):
            self.assertNotIn(gone,source)
        self.assertIn('its terminal</a> answers anything else it asks',source)  # the way out for an unexpected question
        seen=self.drive_dialog('claude','@@ORCHESTRATOR_CONNECT@@{"step":"install","detail":"Installing"}\nLicense: MIT\nAgree to terms? [Y/n]')
        self.assertTrue(seen['agree'])
        self.assertIn('Agree to terms? [Y/n]',seen['question'])
        self.assertFalse(seen['stepsHidden'])  # still getting ready: the checklist stays
