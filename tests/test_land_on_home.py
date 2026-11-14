"""After signing in, the user always lands on Home, even when the address still names another page (a reload, a redirect back from
a sign-in provider, an email link). Reconnecting a page that is already open must leave it where it is."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()


def function_source(name: str, after: str = "") -> str:
    start = APP.index(f"{after}{name}") if after else APP.index(name)
    return APP[start:APP.index("\n}\n", start) + 3]


@unittest.skipUnless(shutil.which("node"), "node is required")
class LandOnHomeBehaviourTests(unittest.TestCase):
    def run_land(self, hash_value: str, replace_throws: bool = False):
        source = function_source("function landOnHome()")
        script = f"""
const calls = [];
const location = {{hash: {json.dumps(hash_value)}, pathname: '/app/', search: '?x=1'}};
const history = {{replaceState: (...args) => {{ if ({str(replace_throws).lower()}) throw new Error('blocked'); calls.push(args); location.hash = '#/'; }}}};
{source}
landOnHome();
console.log(JSON.stringify({{hash: location.hash, calls: calls.map(c => c[2])}}));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_any_other_page_becomes_home_without_adding_a_history_entry(self):
        for page in ("#/tests", "#/jobs/abc123", "#/config/ai", "#/product?x=1", "#/computers"):
            with self.subTest(page=page):
                out = self.run_land(page)
                self.assertEqual(out["hash"], "#/")
                self.assertEqual(out["calls"], ["/app/?x=1#/"])  # replaced, keeping the path and query

    def test_home_and_an_empty_address_are_left_alone(self):
        for page in ("", "#", "#/"):
            with self.subTest(page=page):
                self.assertEqual(self.run_land(page)["calls"], [])

    def test_if_the_browser_refuses_to_replace_the_address_the_hash_is_set_instead(self):
        self.assertEqual(self.run_land("#/tests", replace_throws=True)["hash"], "#/")


class WhichSignInsLandOnHomeTests(unittest.TestCase):
    def test_unlockWith_goes_home_only_when_asked_to(self):
        self.assertIn("async function unlockWith(token, message, { isCurrent = () => true, home = false } = {})", APP)
        body = function_source("async function unlockWith(")
        self.assertIn("if (home) landOnHome();\n  route();", body)

    def test_a_provider_sign_in_google_apple_github_email_and_redirects_lands_on_home(self):
        body = function_source("async function afterProviderSignIn(")
        self.assertIn("{ home: true }", body)

    def test_opening_a_computer_after_signing_in_lands_on_home_unless_an_idea_is_waiting(self):
        body = function_source("async function enterMachine(")
        self.assertIn("{ home: !idea }", body)

    def test_the_access_token_form_lands_on_home(self):
        form = APP[APP.index('toast("Session unlocked");'):][:400]
        self.assertLess(form.index("landOnHome();"), form.index("route();"))

    def test_reconnecting_a_page_that_is_already_open_stays_where_it_is(self):
        reconnect = APP[APP.index("const reconnect = async (manual = false)"):][:900]
        self.assertIn("await unlockWith(res.token, `Opened ${current.name}`, { isCurrent });", reconnect)
        self.assertNotIn("home: true", reconnect)
        quiet = function_source("async function reconnectQuietly(")
        self.assertNotIn("landOnHome", quiet)

    def test_landing_on_home_is_never_a_second_navigation(self):
        self.assertIn("history.replaceState(null", function_source("function landOnHome()"))


if __name__ == "__main__":
    unittest.main()
