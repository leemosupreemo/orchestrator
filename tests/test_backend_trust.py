"""The hosted app must only talk to a computer its account vouched for, whatever a link says."""
import json
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"

SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[2]);
const store = new Map(Object.entries(input.storage));
const context = {
  window: {location: {search: input.search, hostname: input.hostname, pathname: '/', hash: ''}, history: {replaceState() {}}},
  localStorage: {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k)},
  Account: {isHosted: () => input.hosted},
  URLSearchParams,
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const DEFAULT_REMOTE_BACKEND'), source.indexOf('const LAST_LOGIN_KEY')), context);
process.stdout.write(JSON.stringify({backend: vm.runInContext('getBackendUrl()', context), token: vm.runInContext('getToken()', context),
  storedBackend: store.get('orchestrator_backend') ?? null, storedToken: store.get('orchestrator_token') ?? null}));
"""


class BackendTrustTests(unittest.TestCase):
    def run_case(self, **case):
        payload = {"storage": {}, "search": "", "hostname": "swift-orch-web-20260923.web.app", "hosted": True, **case}
        result = subprocess.run(["node", "-e", SCRIPT, str(APP), json.dumps(payload)], check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_a_link_cannot_choose_the_hosted_apps_backend_or_token(self):
        result = self.run_case(search="?backend=https://evil.example&token=stolen")
        self.assertEqual((result["backend"], result["token"]), ("", ""))
        self.assertEqual((result["storedBackend"], result["storedToken"]), (None, None))

    def test_a_backend_the_account_vouched_for_is_used(self):
        url = "https://good.trycloudflare.com"
        result = self.run_case(storage={"orchestrator_backend": url, "orchestrator_backend_account": url})
        self.assertEqual(result["backend"], url)

    def test_a_stored_backend_the_account_did_not_vouch_for_is_dropped(self):
        result = self.run_case(storage={"orchestrator_backend": "https://evil.example"})
        self.assertEqual(result["backend"], "")
        self.assertIsNone(result["storedBackend"])
        mismatch = self.run_case(storage={"orchestrator_backend": "https://evil.example", "orchestrator_backend_account": "https://good.trycloudflare.com"})
        self.assertEqual(mismatch["backend"], "")

    def test_a_computers_own_page_still_accepts_its_token_link(self):
        result = self.run_case(hosted=False, hostname="abc.trycloudflare.com", search="?token=owner-token")
        self.assertEqual(result["token"], "owner-token")


if __name__ == "__main__":
    unittest.main()
