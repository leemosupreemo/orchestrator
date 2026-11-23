"""Starting a server: the project's own rule is shown with a plain explanation of what it is, instead of a bare quote."""
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
DIALOG = APP[APP.index("async function confirmServerAction("):]
DIALOG = DIALOG[:DIALOG.index("\n}\n")]


class ServerDialogRuleTests(unittest.TestCase):
    def test_the_rule_is_quoted_and_labelled_as_the_projects_own(self):
        self.assertIn("<strong>Project rule</strong> (${esc(view.rule.file)}): “${esc(view.rule.sentence)}”", DIALOG)
        self.assertNotIn("Your project says (", DIALOG)

    def test_it_says_the_rule_is_a_reminder_not_enforced(self):
        self.assertIn("Reminder only (not enforced)", DIALOG)

    def test_it_says_what_the_action_does_and_where_succinctly(self):
        self.assertIn("Runs <code>${esc(command)}</code> in <code>${esc(server.dir)}</code> (non-destructive).", DIALOG)
        self.assertIn("only affects containers on ${esc(whereLabel)}.", DIALOG)

    def test_the_verb_follows_the_action_and_no_rule_means_no_notice(self):
        self.assertIn("${verb.toLowerCase()} only affects containers", DIALOG)
        self.assertIn("${view.rule ? ", DIALOG)
        self.assertIn(': ""}`, verb, { compact: true, danger: action === "stop" })', DIALOG)


if __name__ == "__main__":
    unittest.main()
