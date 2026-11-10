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


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class RedirectReturnTests(unittest.TestCase):
    """Execute return handling and the real provider/session exchange, without a provider login."""

    def run_return(self, outcome='success', pending=True, hosted=False, exchange_error=False):
        script = r'''
const fs=require('fs'),vm=require('vm');
const [outcome,pending,hosted,exchangeError]=JSON.parse(process.argv[2]);
const source=fs.readFileSync(process.argv[1],'utf8');
const calls=[],errors=[];
const user={uid:'demo-user',email:'demo@example.invalid',providerData:[{providerId:'google.com'}],
 getIdToken:async()=>{calls.push(['id-token']);return 'isolated-fake-token';}};
const auth={getRedirectResult:async()=>{
 calls.push(['redirect-result']);
 if(outcome==='empty')return {user:null};
 if(outcome==='cancelled')throw {code:'auth/cancelled-popup-request'};
 if(outcome==='storage')throw {code:'auth/internal-error',message:'Unable to process request due to missing initial state. Storage partitioning.'};
 if(outcome==='network')throw {code:'auth/network-request-failed',message:'Connection lost'};
 return {user};
}};
const storage=pending?{'firebase:pendingRedirect:demo':'true'}:{};
storage.getItem=k=>storage[k];
const context={window:{firebase:{auth:()=>auth},location:{hostname:'demo.firebaseapp.com'}},
 sessionStorage:storage,console:{warn:()=>{}},
 refreshState:async()=>{calls.push(['refresh']);},route:()=>calls.push(['route']),
 showSignInGate:m=>errors.push(m),setLastLoginMethod:m=>calls.push(['method',m]),
 Account:{active:()=>hosted},getBackendUrl:()=> 'http://127.0.0.1:8080',
 openAccount:async()=>{calls.push(['account']);vm.runInContext('endSigningIn()',context);},
 api:async(path,request)=>{calls.push(['exchange',path,request]);if(exchangeError)throw new Error('backend offline');return {token:'local-session'};},
 unlockWith:async(token,message)=>{calls.push(['unlock',token,message]);vm.runInContext('endSigningIn()',context);}};
vm.createContext(context);
const providerStart=source.indexOf('async function afterProviderSignIn(');
vm.runInContext(source.slice(providerStart,source.indexOf('// Back from the computers',providerStart)),context);
const start=source.indexOf('// True from the moment');
vm.runInContext(source.slice(start,source.indexOf('// ---------------------------------------------------------------- helpers',start)),context);
setImmediate(()=>console.log(JSON.stringify({calls,errors,signingIn:vm.runInContext('signingIn',context)})));
'''
        result = subprocess.run(['node', '-e', script, str(APP), json.dumps([outcome, pending, hosted, exchange_error])],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_returned_provider_user_exchanges_id_token_then_unlocks_the_computer(self):
        out = self.run_return()
        self.assertEqual(out['calls'], [
            ['redirect-result'], ['method', 'google'], ['id-token'],
            ['exchange', 'auth', {'method': 'POST', 'body': {'id_token': 'isolated-fake-token'}}],
            ['unlock', 'local-session', 'Signed in as demo@example.invalid'],
        ])
        self.assertFalse(out['signingIn'])
        self.assertEqual(out['errors'], [])

    def test_hosted_return_opens_account_without_sending_a_token_to_a_backend(self):
        out = self.run_return(hosted=True)
        self.assertEqual(out['calls'], [['redirect-result'], ['method', 'google'], ['account']])
        self.assertFalse(out['signingIn'])

    def test_empty_or_cancelled_return_resumes_navigation_only_when_pending(self):
        for outcome in ('empty', 'cancelled'):
            for pending in (True, False):
                with self.subTest(outcome=outcome, pending=pending):
                    out = self.run_return(outcome=outcome, pending=pending)
                    self.assertEqual(out['calls'], [['redirect-result']] + ([['refresh'], ['route']] if pending else []))
                    self.assertFalse(out['signingIn'])
                    self.assertEqual(out['errors'], [])

    def test_storage_partition_failure_is_actionable_and_clears_loading(self):
        out = self.run_return(outcome='storage')
        self.assertFalse(out['signingIn'])
        self.assertEqual(out['calls'], [['redirect-result']])
        self.assertIn('blocks the storage', out['errors'][0])

    def test_network_or_backend_failure_clears_loading_and_does_not_unlock(self):
        for kwargs, message in (({'outcome': 'network'}, 'Connection lost'),
                                ({'exchange_error': True}, 'backend offline')):
            with self.subTest(kwargs=kwargs):
                out = self.run_return(**kwargs)
                self.assertFalse(out['signingIn'])
                self.assertFalse(any(call[0] == 'unlock' for call in out['calls']))
                self.assertIn(message, out['errors'][0])


if __name__ == '__main__':
    unittest.main()
