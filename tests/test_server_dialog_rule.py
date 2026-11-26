"""Server action confirmation dialog stays concise and omits the noisy AGENTS.md reminder."""
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
DIALOG = APP[APP.index("async function confirmServerAction("):]
DIALOG = DIALOG[:DIALOG.index("\n}\n")]


class ServerDialogRuleTests(unittest.TestCase):
    def test_dialog_omits_project_rule_reminder(self):
        self.assertNotIn("Project rule", DIALOG)
        self.assertNotIn("Reminder only (not enforced)", DIALOG)
        self.assertNotIn("view.rule", DIALOG)

    def test_it_says_what_the_action_does_and_where_succinctly(self):
        self.assertIn("`${verb} server?`", DIALOG)
        self.assertIn("Runs <code>${esc(command)}</code> on <strong>${esc(where)}</strong>", DIALOG)
        self.assertIn("Containers only — no data is deleted.", DIALOG)
        self.assertIn('verb, { compact: true, danger: action === "stop" })', DIALOG)

    def test_install_docker_modal_explains_missing_docker_and_size(self):
        self.assertIn('if (action === "install_docker")', DIALOG)
        self.assertIn("Docker isn't installed", DIALOG)
        self.assertIn("Would you like to install it now?", DIALOG)
        self.assertIn("~700 MB download", DIALOG)


if __name__ == "__main__":
    unittest.main()
