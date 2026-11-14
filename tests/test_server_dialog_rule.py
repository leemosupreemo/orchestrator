"""Starting a server: the project's own rule is shown with a plain explanation of what it is, instead of a bare quote."""
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
DIALOG = APP[APP.index("async function confirmServerAction("):]
DIALOG = DIALOG[:DIALOG.index("\n}\n")]


class ServerDialogRuleTests(unittest.TestCase):
    def test_the_rule_is_quoted_and_labelled_as_the_projects_own(self):
        self.assertIn("<strong>A rule from your project</strong> (${esc(view.rule.file)}): “${esc(view.rule.sentence)}”", DIALOG)
        self.assertNotIn("Your project says (", DIALOG)

    def test_it_says_what_the_rule_is_and_that_orchestrator_does_not_check_it(self):
        for bit in ("your project's own note about protecting a live or production server", "Orchestrator only shows it; it doesn't check it"):
            self.assertIn(bit, DIALOG)

    def test_it_says_what_the_action_does_and_where_and_to_check_it_is_the_right_computer(self):
        self.assertIn("changes nothing but the containers on ${esc(whereLabel)}", DIALOG)
        self.assertIn("check that is the computer you mean, and not a live one", DIALOG)

    def test_the_verb_follows_the_action_and_no_rule_means_no_notice(self):
        self.assertIn("${verb} here changes nothing", DIALOG)
        self.assertIn("${view.rule ? ", DIALOG)
        self.assertIn(': ""}`, verb, { compact: true, danger: action === "stop" })', DIALOG)


if __name__ == "__main__":
    unittest.main()
