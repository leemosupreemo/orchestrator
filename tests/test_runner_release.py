"""Hosted install/update instructions must obtain the matching runner release."""
import json
import subprocess
import unittest
from pathlib import Path
from orchestrator import account

STATIC = Path(__file__).resolve().parents[1] / "orchestrator/web/static"

class RunnerReleaseTests(unittest.TestCase):
    def test_hosted_release_detects_pre_feature_runners_and_installs_web_ui(self):
        js = STATIC / "account.js"
        out = subprocess.check_output(["node", "-e", f"require({json.dumps(str(STATIC / "html.js"))}); require({json.dumps(str(js))}); const A=globalThis.Account; console.log(JSON.stringify({{old:A.outdated(2), current:A.outdated({account.API_VERSION}), install:A.INSTALL_COMMAND, hint:A.UPDATE_HINT}}));"],text=True)
        state = json.loads(out)
        self.assertTrue(state["old"])
        self.assertFalse(state["current"])
        self.assertTrue(account.INSTALL_SPEC.endswith("@web-ui"))
        self.assertIn(account.INSTALL_SPEC, state["install"])
        self.assertIn(account.INSTALL_SPEC, state["hint"])
        self.assertIn("restart",state["hint"].lower())

    def test_quick_installer_defaults_to_same_release_as_updater(self):
        out = subprocess.check_output(["sh", str(STATIC / "install.sh"), "--dry-run"],text=True)
        self.assertIn(account.INSTALL_SPEC, out)
        self.assertTrue(account.INSTALL_SPEC.endswith("@web-ui"))

    def test_project_helper_install_advice_matches_hosted_release(self):
        from orchestrator.cli import helper_script
        self.assertIn(account.INSTALL_SPEC, helper_script())
