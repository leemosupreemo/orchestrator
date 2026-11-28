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
        self.assertLess(body.index("landOnHome()"), body.index("if (Account.active()) return openAccount();"))

    def test_opening_a_computer_after_signing_in_lands_on_home_unless_an_idea_is_waiting(self):
        body = function_source("async function enterMachine(")
        self.assertIn("{ home: !idea && !hash }", body)

    def test_the_access_token_form_lands_on_home(self):
        form = APP[APP.index('toast("Session unlocked");'):][:400]
        self.assertLess(form.index("landOnHome();"), form.index("route();"))

    def test_reconnecting_a_page_that_is_already_open_stays_where_it_is(self):
        start = APP.index("const reconnect = async (manual = false)")
        reconnect = APP[start:APP.index("reconnecting = task;", start)]
        self.assertIn("await unlockWith(res.token, `Opened ${current.name}`, { isCurrent });", reconnect)
        self.assertNotIn("home: true", reconnect)
        quiet = function_source("async function reconnectQuietly(")
        self.assertNotIn("landOnHome", quiet)

    def test_landing_on_home_is_never_a_second_navigation(self):
        self.assertIn("history.replaceState(null", function_source("function landOnHome()"))


@unittest.skipUnless(shutil.which("node"), "node is required")
class InitialLoadTests(unittest.TestCase):
    """Loading the app always starts on Home; only email sign-in, pairing, and alert-notification links keep what they carry."""

    def run_load(self, hash_value: str, search: str = ""):
        load_fn = function_source("function landOnHomeOnLoad()")
        home_fn = function_source("function landOnHome()")
        script = f"""
const location = {{hash: {json.dumps(hash_value)}, pathname: '/app/', search: {json.dumps(search)}}};
const history = {{replaceState: (...a) => {{ location.hash = '#/'; }}}};
{home_fn}
{load_fn}
landOnHomeOnLoad();
console.log(JSON.stringify(location.hash));
"""
        return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)

    def test_loading_any_page_goes_home(self):
        for page in ("#/tests", "#/jobs/abc", "#/config/ai", "#/delivery?branch=main", "#/product", "#/computers"):
            with self.subTest(page=page):
                self.assertEqual(self.run_load(page), "#/")

    def test_an_email_sign_in_link_is_not_stripped_on_load(self):
        for link in ("#/?apiKey=abc&oobCode=def&mode=signIn", "#/x?oobCode=1", "#/?mode=signIn"):
            with self.subTest(link=link):
                self.assertEqual(self.run_load(link), link)

    def test_a_pairing_link_or_alert_notification_link_keeps_its_route(self):
        self.assertEqual(self.run_load("#/connect?code=ABCD-2345"), "#/connect?code=ABCD-2345")
        self.assertEqual(self.run_load("#/jobs/abc", search="?machine=mac-1"), "#/jobs/abc")

    def test_it_runs_before_the_sign_in_handling_so_that_starts_from_home(self):
        self.assertLess(APP.index("landOnHomeOnLoad();"), APP.index("window.firebase.auth().getRedirectResult()"))


if __name__ == "__main__":
    unittest.main()
