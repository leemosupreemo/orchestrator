"""Web boundaries for direct dependency updates and shared Product content."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_web_ui import ServerTestCase, UI_HEADERS
from orchestrator.web import server
from orchestrator import prd


class DirectPackageActionTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        (self.root/'package.json').write_text(json.dumps({'dependencies':{'left-pad':'^1.3.0'}}))

    def test_native_update_action_is_owner_only_and_uses_validated_manifest_dependency(self):
        self.assertIn('package_update',server.OWNER_ONLY_ACTIONS)
        argv=server.build_package_update({'ecosystem':'npm','name':'left-pad','manifest':'package.json'},self.root)
        self.assertIn('update_package.py',argv)
        self.assertEqual(argv[-6:],['--ecosystem','npm','--name','left-pad','--manifest','package.json'])
        for name in ('missing','--help','left-pad; echo bad'):
            with self.subTest(name=name), self.assertRaises(server.UIError):
                server.build_package_update({'ecosystem':'npm','name':name,'manifest':'package.json'},self.root)

    def test_packages_explain_native_capability_before_starting_an_update(self):
        (self.root/'build.gradle').write_text('dependencies {\n implementation "com.example:sample:1.0"\n}')
        _, data=self.request('GET','/api/packages')
        entries={i['ecosystem']:i for i in data['installed']}
        self.assertTrue(entries['npm']['update']['supported'])
        self.assertEqual(entries['npm']['update']['manager'],'npm')
        self.assertFalse(entries['gradle']['update']['supported'])
        self.assertIn('Gradle',entries['gradle']['update']['reason'])

    def test_members_cannot_start_package_manager_commands(self):
        from tests.test_web_ui import SignInTests
        token=SignInTests.sign_in(self,source='settings')
        res,_=SignInTests.as_user(self,token,'POST','/api/runs',body={'action':'package_update','params':{'ecosystem':'npm','name':'left-pad','manifest':'package.json'}},sources={'tester@example.com':'settings'})
        self.assertEqual(res.status,403)

    def test_product_source_and_design_endpoints_use_shared_asset_storage(self):
        doc=prd.Prd(self.root,server.runtime_dir(self.root))
        designs=doc.asset_folder('designs');designs.mkdir(parents=True,exist_ok=True)
        (designs/'sample.png').write_bytes(b'png')
        sources=doc.asset_folder('source');sources.mkdir(parents=True,exist_ok=True)
        (sources/'brief.txt').write_text('Shared source')
        res,data=self.request('GET','/api/product/design/sample.png')
        self.assertEqual(res.status,200)
        self.assertEqual(data,b'png')
        res,data=self.request('GET','/api/product/source/brief.txt')
        self.assertEqual(res.status,200)
        self.assertEqual(data,b'Shared source')


class PackageResultTests(unittest.TestCase):
    def test_package_update_result_is_a_human_message_instead_of_protocol_json(self):
        from orchestrator import run_result
        for ok, code, message in ((True, 0, 'react: native update and project tests passed.'), (False, 1, 'Tests failed after updating react.')):
            with self.subTest(ok=ok):
                output='native command output\nORCHESTRATOR_PACKAGE_UPDATE_RESULT='+json.dumps({'ok':ok,'message':message,'stage':'complete' if ok else 'tests'})
                result=run_result.summarize('package_update',output,code)
                self.assertEqual(result['headline'],message)
                self.assertEqual(result['tone'],'ok' if ok else 'bad')
