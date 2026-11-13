"""The password-recovery page (orchestrator/web/static/reset-password.js), run under node with a fake Firebase auth object."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
MODULE = STATIC / "reset-password.js"


@unittest.skipUnless(shutil.which("node"), "node is needed to test the front-end modules")
class ResetPasswordLogicTests(unittest.TestCase):
    def run_js(self, body: str):
        script = f"""
const R = require(process.argv[1]);
const failing = (code) => ({{ code }});
const fake = (overrides = {{}}) => {{
  const calls = [];
  const auth = {{
    sendPasswordResetEmail: async (e) => {{ calls.push(["send", e]); }},
    verifyPasswordResetCode: async (c) => {{ calls.push(["verify", c]); return "ana@example.com"; }},
    confirmPasswordReset: async (c, p) => {{ calls.push(["confirm", c, p]); }},
    ...overrides,
  }};
  return {{ auth, calls }};
}};
(async () => {{
{body}
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        res = subprocess.run(["node", "-e", script, str(MODULE)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_reads_the_mode_and_code_firebase_puts_on_the_link(self):
        out = self.run_js("""
console.log(JSON.stringify([R.readLink("?mode=resetPassword&oobCode=abc&apiKey=k&lang=en"), R.readLink(""), R.readLink("?mode=verifyEmail&oobCode=z")]));
""")
        self.assertEqual(out, [{"mode": "resetPassword", "code": "abc"}, {"mode": "", "code": ""}, {"mode": "verifyEmail", "code": "z"}])

    def test_an_address_with_no_account_gets_the_same_answer_as_one_with_an_account(self):
        out = self.run_js("""
const known = await R.requestReset(fake().auth, " ana@example.com ");
const unknown = await R.requestReset(fake({ sendPasswordResetEmail: async () => { throw failing("auth/user-not-found"); } }).auth, "nobody@example.com");
console.log(JSON.stringify({ known, unknown }));
""")
        self.assertTrue(out["known"]["ok"] and out["unknown"]["ok"])
        self.assertEqual(out["known"]["message"], out["unknown"]["message"].replace("nobody", "ana"))
        self.assertIn("If an account exists for ana@example.com", out["known"]["message"])

    def test_real_problems_sending_are_not_hidden_behind_the_neutral_answer(self):
        out = self.run_js("""
const say = async (code) => (await R.requestReset(fake({ sendPasswordResetEmail: async () => { throw failing(code); } }).auth, "a@b.co"));
console.log(JSON.stringify({
  empty: await R.requestReset(fake().auth, "  "),
  bad: await say("auth/invalid-email"), busy: await say("auth/too-many-requests"),
  offline: await say("auth/network-request-failed"), other: await say("auth/internal-error"),
}));
""")
        for key in ("empty", "bad", "busy", "offline", "other"):
            self.assertFalse(out[key]["ok"], key)
        self.assertEqual(len({out[k]["message"] for k in out}), 5)  # each says something different
        self.assertNotIn("auth/", json.dumps(out))  # nothing raw from Firebase reaches the person

    def test_a_good_link_gives_the_email_and_a_dead_one_asks_for_a_new_link(self):
        out = self.run_js("""
const good = await R.checkCode(fake().auth, "abc");
const dead = async (code) => R.checkCode(fake({ verifyPasswordResetCode: async () => { throw failing(code); } }).auth, "abc");
console.log(JSON.stringify({ good, expired: await dead("auth/expired-action-code"), used: await dead("auth/invalid-action-code"), other: await dead("auth/internal-error") }));
""")
        self.assertEqual(out["good"], {"ok": True, "email": "ana@example.com"})
        for key in ("expired", "used"):
            self.assertTrue(out[key]["dead"], key)
            self.assertIn("expired or was already used", out[key]["message"])
        self.assertNotIn("dead", out["other"])

    def test_the_new_password_is_checked_here_before_firebase_is_asked(self):
        out = self.run_js("""
const { auth, calls } = fake();
const short = await R.setPassword(auth, "abc", "short", "short");
const mismatch = await R.setPassword(auth, "abc", "longenough1", "longenough2");
console.log(JSON.stringify({ short, mismatch, calls }));
""")
        self.assertEqual((out["short"]["field"], out["mismatch"]["field"]), ("password", "confirm"))
        self.assertEqual(out["calls"], [])  # neither reached Firebase

    def test_saves_the_password_trimmed_because_the_sign_in_form_trims_it(self):
        out = self.run_js("""
const { auth, calls } = fake();
const result = await R.setPassword(auth, "abc", "  correct horse  ", "correct horse");
console.log(JSON.stringify({ result, calls }));
""")
        self.assertEqual(out["result"], {"ok": True})
        self.assertEqual(out["calls"], [["confirm", "abc", "correct horse"]])

    def test_failures_saving_say_what_to_do(self):
        out = self.run_js("""
const fail = async (code) => R.setPassword(fake({ confirmPasswordReset: async () => { throw failing(code); } }).auth, "abc", "longenough1", "longenough1");
console.log(JSON.stringify({ weak: await fail("auth/weak-password"), expired: await fail("auth/expired-action-code"), other: await fail("auth/internal-error") }));
""")
        self.assertEqual(out["weak"]["field"], "password")
        self.assertTrue(out["expired"]["dead"])
        self.assertFalse(out["other"]["ok"])
        self.assertNotIn("auth/", json.dumps(out))


class ResetPasswordPageTests(unittest.TestCase):
    def test_the_page_has_one_external_script_of_its_own_and_no_inline_script(self):
        page = (STATIC / "reset-password.html").read_text()
        self.assertIn('<script src="reset-password.js"></script>', page)  # the server's policy blocks inline scripts
        self.assertEqual(page.count("<script"), 3)  # Firebase app, Firebase auth, this page
        self.assertNotIn("oobCode", page)
        self.assertIn('<meta name="referrer" content="no-referrer">', page)  # the one-time code never leaves in a Referer

    def test_the_sign_in_form_uses_the_same_logic_so_both_say_the_same_thing(self):
        index = (STATIC / "index.html").read_text()
        app = (STATIC / "app.js").read_text()
        self.assertLess(index.index("reset-password.js"), index.index("app.js"))
        self.assertIn("PasswordReset.requestReset(firebase.auth(), email)", app)
        self.assertNotIn("sendPasswordResetEmail", app)  # nothing in the app talks to Firebase for this directly any more


if __name__ == "__main__":
    unittest.main()
