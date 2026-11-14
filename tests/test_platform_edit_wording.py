"""The platforms card on the Tests page and the dialog that changes it: said in plain words, with the reason to use it.

"Edit platforms" sat in the card header with no explanation, so it wasn't clear what it changed or why anyone would reshape the
parts Orchestrator had found."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
APP = (STATIC / "app.js").read_text()


def between(start: str, end: str) -> str:
    i = APP.index(start)
    return APP[i:APP.index(end, i)]


class PlatformCardTests(unittest.TestCase):
    card = between("function platformCoverageHtml(", "// What Orchestrator found for testing")

    def test_the_card_says_what_it_shows_and_where_the_parts_came_from(self):
        self.assertIn("Your project split into parts, with test numbers for each.", self.card)
        self.assertIn("Found automatically from your folders.", self.card)
        self.assertIn("You set these parts yourself.", self.card)

    def test_the_way_to_change_it_is_a_question_beside_the_explanation_not_an_unexplained_header_button(self):
        header = self.card[:self.card.index('<div class="card-b')]
        self.assertNotIn("edit-platforms", header)
        note = self.card[self.card.index('id="platform-split-note"'):]
        self.assertIn("Does a part look wrong?", note)
        self.assertIn('id="edit-platforms"', note)
        self.assertIn("Change how it's split", note)
        self.assertNotIn(">Edit platforms<", self.card)

    def test_only_an_owner_is_offered_the_change(self):
        self.assertIn("${owner ? ` Does a part look wrong?", self.card)


class PlatformDialogTests(unittest.TestCase):
    dialog = between("async function editPlatforms(", "function collectPlatforms(")

    def test_it_is_named_for_what_it_does(self):
        self.assertIn('formDialog("Change how your project is split"', self.dialog)
        self.assertNotIn('formDialog("Edit platforms"', self.dialog)

    def test_it_explains_what_a_platform_is_and_why_you_might_change_it(self):
        for bit in ("called platforms", "Change this only if a part looks wrong", "a server counted as part of the app",
                    "It only changes how the test numbers are grouped", "doesn't touch your code or how tests run"):
            self.assertIn(bit, self.dialog)

    def test_the_columns_are_labelled_and_the_buttons_say_what_they_do(self):
        for bit in (">Name<", ">What it is<", "Folders it covers, separated by commas", "Add another part", "To remove a part, clear its name.",
                    "Go back to finding the parts automatically from your folders"):
            self.assertIn(bit, self.dialog)

    def test_the_labels_line_up_with_the_row_and_are_hidden_on_phones(self):
        css = (STATIC / "style.css").read_text()
        row = re.search(r"\.plat-edit-row \{ display: grid; grid-template-columns: ([^;]+);", css).group(1)
        head = re.search(r"\.plat-edit-head \{ display: grid; grid-template-columns: ([^;]+);", css).group(1)
        self.assertEqual(row, head)
        self.assertRegex(css, r"@media \(max-width: 640px\) \{[^\n]*\.plat-edit-head \{ display: none; \}")

    def test_what_the_dialog_collects_is_unchanged(self):
        self.assertIn('name="reset"', self.dialog)
        self.assertIn("platformRowHtml(", self.dialog)
        self.assertIn('id="platform-add-row"', self.dialog)

    def test_the_user_guide_uses_the_new_name(self):
        guide = (STATIC / "docs" / "user-guide.md").read_text()
        self.assertIn("**Change how it's split**", guide)
        self.assertNotIn("**Edit platforms**", guide)


if __name__ == "__main__":
    unittest.main()
