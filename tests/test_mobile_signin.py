"""Run the app's actual sign-in handlers against controlled browser and Firebase boundaries."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'orchestrator/web/static/app.js'


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class MobileSignInTests(unittest.TestCase):
    def run_flow(self, ua, platform='', touches=0, fail=None, host='swift-orch-web-20260923.web.app'):
        script = r'''
const fs=require('fs'),vm=require('vm');
const [ua,platform,touches,fail,host]=JSON.parse(process.argv[2]);
const source=fs.readFileSync(process.argv[1],'utf8');
const calls=[],moves=[],handlers={},errors=[];
const btn={disabled:false,innerHTML:'Sign in',addEventListener:(name,fn)=>handlers[name]=fn};
const auth={signInWithPopup:async p=>{calls.push(['popup',p]); if(fail)throw {code:fail,message:'provider failed'};return {user:{uid:'user'}};},
 signInWithRedirect:async p=>{calls.push(['redirect',p]);if(fail)throw {code:fail,message:'provider failed'};}};
const location={hostname:host,origin:'https://'+host,pathname:'/',search:'?machine=m_test',hash:'#/jobs/example',replace:u=>moves.push(u)};
const context={navigator:{userAgent:ua,platform,maxTouchPoints:touches},URL,location,
 window:{location,firebase:{apps:[{}],auth:()=>auth}},firebase:{auth:()=>auth},
 $:()=>btn,syncBackend:()=>{},setLastLoginMethod:()=>{},toast:m=>errors.push(m),
 showSigningIn:()=>{},afterProviderSignIn:async u=>calls.push(['connected',u.uid]),
 endSigningIn:()=>calls.push(['end']),showSignInGate:m=>errors.push(m),
 signingIn:false,Account:{HOSTED_ORIGINS:['https://swift-orch-web-20260923.web.app']}};
vm.createContext(context);
vm.runInContext(source.slice(0,source.indexOf('// True from the moment')),context);
const start=source.indexOf('  const wireSso = '),end=source.indexOf('  $("#redirect-fallback")',start);
vm.runInContext(source.slice(start,end)+'\nwireSso("#google-signin-btn","Google",()=>({providerId:"google.com"}));',context);
(async()=>{await handlers.click();console.log(JSON.stringify({calls,moves,errors,disabled:btn.disabled}));})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(APP), json.dumps([ua, platform, touches, fail, host])],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_mobile_providers_use_same_tab_redirect_without_starting_a_popup(self):
        for ua, platform, touches in [('Mozilla/5.0 (iPhone) AppleWebKit Safari', 'iPhone', 5),
                                     ('Mozilla/5.0 (Linux; Android 15) Chrome', 'Linux armv8l', 5),
                                     ('Mozilla/5.0 (Macintosh) AppleWebKit Safari', 'MacIntel', 5)]:
            with self.subTest(ua=ua):
                out = self.run_flow(ua, platform, touches)
                self.assertEqual(out['calls'], [['redirect', {'providerId': 'google.com'}]])
                self.assertEqual(out['errors'], [])

    def test_desktop_keeps_popup_and_completes_the_session_exchange(self):
        out = self.run_flow('Mozilla/5.0 (Macintosh) Safari', 'MacIntel', 0)
        self.assertEqual(out['calls'], [['popup', {'providerId': 'google.com'}], ['connected', 'user']])
        self.assertEqual(out['moves'], [])

    def test_mobile_host_matches_auth_domain_preserving_machine_and_route(self):
        out = self.run_flow('iPhone', 'iPhone', 5)
        self.assertEqual(out['moves'], ['https://swift-orch-web-20260923.firebaseapp.com/?machine=m_test#/jobs/example'])

    def test_canonical_auth_host_and_direct_computer_connections_do_not_redirect_again(self):
        for host in ('swift-orch-web-20260923.firebaseapp.com', 'localhost', 'my-machine.trycloudflare.com'):
            with self.subTest(host=host):
                self.assertEqual(self.run_flow('iPhone', 'iPhone', 5, host=host)['moves'], [])

    def test_mobile_redirect_failure_resets_signing_in_and_reports_error(self):
        out = self.run_flow('iPhone', 'iPhone', 5, fail='auth/network-request-failed')
        self.assertEqual(out['calls'][0][0], 'redirect')
        self.assertIn(['end'], out['calls'])
        self.assertIn('provider failed', out['errors'])


if __name__ == '__main__':
    unittest.main()
