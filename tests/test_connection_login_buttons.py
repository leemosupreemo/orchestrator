"""Connection pages: the button that opens the provider's own site just says "Login". What it does is in the heading above it, and
screen readers still get the whole phrase from its accessible name."""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
_START = APP.index("const figmaOptionA = p.id")
DIALOG = APP[_START:APP.index("const dialogPromise = formDialog(", _START)]


def button(identifier: str) -> str:
    start = DIALOG.rindex("<", 0, DIALOG.index(identifier))
    end = DIALOG.index("</", DIALOG.index(identifier))
    return DIALOG[start:DIALOG.index(">", end) + 1]  # through the closing tag


class LoginButtonTests(unittest.TestCase):
    CASES = {
        'id="figma-dialog-quick-btn"': "Log in and authorize with Figma",
        'id="trello-dialog-quick-btn"': "Log in and authorize with Trello",
        'id="sentry-signin-btn"': "Sign in with Sentry",
        'href="https://id.atlassian.com/manage-profile/security/api-tokens"': "Log in to Atlassian and create an API token",
        'href="${esc(p.token_url)}"': "Log in to ${esc(p.name)} and create a token",
    }

    def test_each_login_button_says_only_login(self):
        for marker, full_name in self.CASES.items():
            with self.subTest(button=marker):
                html = button(marker)
                self.assertEqual(re.sub(r"<[^>]+>", "", html).strip(), "Login ↗")

    def test_the_whole_phrase_is_still_its_accessible_name(self):
        for marker, full_name in self.CASES.items():
            with self.subTest(button=marker):
                self.assertIn(f'aria-label="{full_name}"', button(marker))

    def test_the_long_labels_are_gone(self):
        for old in ("Log in &amp; authorize with Figma ↗", "Log in &amp; authorize with Trello ↗", "Sign in with Sentry ↗",
                    "Log in to Atlassian &amp; create token", "Create a token ↗"):
            self.assertNotIn(old, DIALOG)

    def test_what_each_one_is_for_stays_in_the_text_around_it(self):
        for kept in ("<strong>Log in &amp; authorize with Figma</strong>", "<strong>Log in &amp; authorize with Trello</strong>",
                     "<strong>Sign in with Sentry</strong>", "<strong>1. Create an API token</strong>", 'Click "Create API token" and copy it.'):
            self.assertIn(kept, DIALOG)


if __name__ == "__main__":
    unittest.main()
