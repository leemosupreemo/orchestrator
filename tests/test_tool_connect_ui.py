"""Structured Connect progress and safe browser approval links."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

MODULE=Path(__file__).resolve().parents[1]/'orchestrator/web/static/tool-connect.js'

@unittest.skipUnless(shutil.which('node'),'Node.js required')
class ToolConnectUiTests(unittest.TestCase):
    def state(self,text):
        result=subprocess.run(['node','-e','require(process.argv[1]);console.log(JSON.stringify(ToolConnect.progressState(JSON.parse(process.argv[2]))));',str(MODULE),json.dumps(text)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_progress_skips_no_steps_and_keeps_ready_state(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"check","detail":"Checking"}\n@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Approve in browser"}\n@@ORCHESTRATOR_CONNECT@@{"step":"ready","detail":"Connected"}\n')
        self.assertEqual(result['step'],'ready')
        self.assertEqual(result['detail'],'Connected')

    def test_only_real_https_provider_links_are_offered(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"sign_in","detail":"Sign in"}\nhttps://auth.openai.com/authorize?code=ABC javascript:alert(1) https://auth.openai.com.evil.test/steal https://evil.test/openai.com\n')
        self.assertEqual(result['urls'],['https://auth.openai.com/authorize?code=ABC'])

    def test_installer_terms_require_explicit_agreement(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@{"step":"install","detail":"Installing"}\nAgree to terms? [Y/n]')
        self.assertTrue(result['needsAgreement'])
        self.assertEqual(result['step'],'install')

    def test_malformed_markers_do_not_break_progress_or_turn_into_instructions(self):
        result=self.state('@@ORCHESTRATOR_CONNECT@@bad json\n@@ORCHESTRATOR_CONNECT@@{"step":"shell","detail":"run me"}\n')
        self.assertEqual(result['step'],'check')
        self.assertEqual(result['urls'],[])
